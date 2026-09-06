"""Cluster health endpoints — issue #402 ("Cluster → Overview" dashboard).

Mounted at ``/api/v1/appliance/cluster``:

    GET  /health          one-shot snapshot (initial paint, MCP, scripting)
    GET  /health/stream   SSE — a fresh snapshot every ~2 s (live dashboard)

Both read the k3s cluster *underneath* the appliance via the api pod's
ServiceAccount (nodes + pods + kubelet Summary API). Live CPU / memory — and,
since Kubernetes 1.36, PSI stall percentages (#983) — come from the kubelet
Summary API because the appliance ships no metrics-server / Prometheus, the
same source the TTY console uses. That API is reached per node either
directly (``nodes/stats``) or through the apiserver proxy (``nodes/proxy``);
the snapshot reports which. See ``services/appliance/cluster_health.py`` for
the gather.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import anyio
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB
from app.core.permissions import require_permission
from app.core.responses import EventStreamResponse
from app.db import AsyncSessionLocal
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.services.appliance import k8s
from app.services.appliance.cluster_health import cluster_unavailable, get_cluster_health

logger = structlog.get_logger(__name__)

router = APIRouter()

# How often the SSE stream re-gathers + pushes a snapshot. 2 s is the sweet
# spot: kubelet's Summary API itself only refreshes every ~1–10 s, and a
# handful of kubeapi calls per tick is cheap, but it feels live in the UI.
_STREAM_INTERVAL_S = 2.0


class HostPartition(BaseModel):
    mount: str
    label: str
    total_bytes: int
    used_bytes: int


class PSIWindow(BaseModel):
    """One /proc/pressure line's rolling averages, as percentages of wall time.

    ``avg10`` is the number to look at during an incident; ``avg300`` is the
    one that separates a burst from a condition.
    """

    avg10: float | None = None
    avg60: float | None = None
    avg300: float | None = None


class PSIStats(BaseModel):
    """``some`` / ``full`` stall shares for one resource (#983 Phase 2).

    ``some`` — at least one task was stalled waiting for the resource.
    ``full`` — every runnable task was. At node level the kernel reports CPU
    ``full`` as 0, so a CPU verdict has to read ``some``.

    The whole object is null when the kubelet did not report PSI (pre-1.36,
    or the feature off). Null is UNRECORDED, never "no pressure" — the two
    are opposite facts and a panel that conflates them is worse than one that
    shows nothing.
    """

    some: PSIWindow | None = None
    full: PSIWindow | None = None


class KubeletTransport(BaseModel):
    """Which transport served the kubelet Summary API, per node (#983 Phase 2).

    ``direct`` = straight to the kubelet on :10250, authorized by
    ``nodes/stats``. ``proxy`` = through the apiserver, authorized by
    ``nodes/proxy``, which grants read access to EVERY kubelet endpoint and is
    the grant this exists to retire.

    Per node rather than a single value, because a mixed cluster is the
    dangerous case: one value would report whichever node was processed last,
    and "direct" while another node fell back is precisely the wrong answer to
    "can I drop the broad grant?".

    ``all_direct`` is that decision, and it is False when nothing was probed —
    measuring nothing must never read as safe.

    Reported by whichever api replica served the request. Each replica probes
    every node within that request, so the map is complete; only the
    retry-backoff cache is per-replica.
    """

    by_node: dict[str, str] = {}
    direct_nodes: int = 0
    proxy_nodes: int = 0
    all_direct: bool = False
    blocked_reasons: dict[str, str] = {}


class NodeVitals(BaseModel):
    name: str
    ready: bool
    roles: list[str]
    schedulable: bool
    kubelet_version: str | None = None
    os_image: str | None = None
    kernel: str | None = None
    container_runtime: str | None = None
    architecture: str | None = None
    internal_ip: str | None = None
    age_seconds: int | None = None
    memory_pressure: bool = False
    disk_pressure: bool = False
    pid_pressure: bool = False
    cpu_capacity_cores: float | None = None
    memory_capacity_bytes: int | None = None
    pods_capacity: int | None = None
    pods_running: int = 0
    cpu_usage_cores: float | None = None
    memory_working_set_bytes: int | None = None
    memory_available_bytes: int | None = None
    fs_used_bytes: int | None = None
    fs_capacity_bytes: int | None = None
    # #983 Phase 2 — PSI. null means the kubelet did not report it.
    psi_cpu: PSIStats | None = None
    psi_memory: PSIStats | None = None
    psi_io: PSIStats | None = None
    # #402 — host partitions (root slot / var / ESP) from the supervisor.
    host_disk_partitions: list[HostPartition] = []


class PodSummary(BaseModel):
    name: str
    namespace: str
    component: str | None = None
    node: str | None = None
    phase: str
    state: str
    ready: str
    restarts: int
    age_seconds: int | None = None
    cpu_usage_cores: float | None = None
    memory_working_set_bytes: int | None = None


class WorkloadHealth(BaseModel):
    component: str
    kind: str | None = None
    ready: int
    total: int
    restarts: int
    status: str


class ClusterHealth(BaseModel):
    available: bool
    detail: str | None = None
    nodes_total: int
    nodes_ready: int
    pods_total: int
    pods_running: int
    pods_by_phase: dict[str, int]
    kubelet_version: str | None = None
    is_ha: bool
    control_plane_nodes: int
    metrics_available: bool
    kubelet_transport: KubeletTransport | None = None
    cpu_usage_cores: float | None = None
    cpu_capacity_cores: float | None = None
    memory_working_set_bytes: int | None = None
    memory_capacity_bytes: int | None = None
    nodes: list[NodeVitals]
    workloads: list[WorkloadHealth]
    top_pods_cpu: list[PodSummary]
    top_pods_mem: list[PodSummary]


async def _merge_host_partitions(db: AsyncSession, snap: dict[str, Any]) -> None:
    """Attach supervisor-reported host partitions to each node in ``snap``.

    The api pod is a container and can't see host partitions — the supervisor
    statvfs's them and ships them inside its ``cluster_health`` JSONB (#402).
    We match by hostname == kube node name. Mutates ``snap`` in place.
    """
    if not snap.get("available") or not snap.get("nodes"):
        return
    rows = (
        await db.execute(
            select(Appliance.hostname, Appliance.cluster_health).where(
                Appliance.state == APPLIANCE_STATE_APPROVED
            )
        )
    ).all()
    pmap: dict[str, list[dict[str, Any]]] = {}
    for hostname, ch in rows:
        if hostname and isinstance(ch, dict):
            parts = ch.get("host_disk_partitions")
            if isinstance(parts, list) and parts:
                pmap[hostname] = parts
    for node in snap["nodes"]:
        if node["name"] in pmap:
            node["host_disk_partitions"] = pmap[node["name"]]


@router.get(
    "/health",
    response_model=ClusterHealth,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Cluster health snapshot (nodes + pods + live usage)",
)
async def cluster_health(db: DB) -> ClusterHealth:
    try:
        # Off-loop: the gather is a handful of blocking stdlib kubeapi calls.
        snap = await anyio.to_thread.run_sync(get_cluster_health)
    except k8s.KubeapiUnavailableError as exc:
        # Generic client-facing detail; log the exception text server-side so
        # an internal error message can't reach the operator's browser
        # (CodeQL py/stack-trace-exposure).
        logger.info("cluster_health_kubeapi_unreachable", error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "kubeapi unreachable from api; retry shortly.",
        ) from exc
    await _merge_host_partitions(db, snap)
    return ClusterHealth(**snap)


@router.get(
    "/health/stream",
    # ``response_class`` REPLACES the documented application/json (#921);
    # a ``responses={200: {"content": ...}}`` entry merges with it instead,
    # leaving the route declaring a JSON body it never produces.
    response_class=EventStreamResponse,
    responses={200: {"description": "Server-sent event stream of cluster health"}},
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Stream cluster health snapshots as SSE (~2s cadence)",
)
async def cluster_health_stream(request: Request) -> StreamingResponse:
    """Push a fresh cluster snapshot every ``_STREAM_INTERVAL_S`` seconds.

    Server-driven (no client polling): the browser opens one connection and
    animates each frame. A momentarily-unreachable kubeapi emits an
    ``available: false`` frame (with a reason) rather than dropping the
    stream, so the dashboard self-heals when the control plane settles.
    """

    async def event_source():
        while True:
            if await request.is_disconnected():
                break
            try:
                snap = await anyio.to_thread.run_sync(get_cluster_health)
                # Short-lived session per tick (don't hold a connection open
                # for the whole stream); cheap single-row-per-node lookup.
                async with AsyncSessionLocal() as db:
                    await _merge_host_partitions(db, snap)
            except k8s.KubeapiUnavailableError as exc:
                # Log detail server-side; keep the client-facing reason generic
                # so an exception message can't leak internals to the browser
                # (CodeQL py/stack-trace-exposure).
                logger.info("cluster_health_stream_kubeapi_unreachable", error=str(exc))
                snap = cluster_unavailable("kubeapi unreachable; retrying")
            except Exception as exc:  # noqa: BLE001 — never let the stream die
                logger.warning("cluster_health_stream_gather_failed", error=str(exc))
                snap = cluster_unavailable("health gather failed; retrying")
            yield f"data: {json.dumps(snap)}\n\n"
            await asyncio.sleep(_STREAM_INTERVAL_S)

    # X-Accel-Buffering disables nginx buffering so each frame arrives ASAP —
    # same pattern as the container-log + AI-chat SSE surfaces.
    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )
