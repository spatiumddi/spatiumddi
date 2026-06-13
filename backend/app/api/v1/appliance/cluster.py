"""Cluster health endpoints — issue #402 ("Cluster → Overview" dashboard).

Mounted at ``/api/v1/appliance/cluster``:

    GET  /health          one-shot snapshot (initial paint, MCP, scripting)
    GET  /health/stream   SSE — a fresh snapshot every ~2 s (live dashboard)

Both read the k3s cluster *underneath* the appliance via the api pod's
ServiceAccount (nodes + pods + kubelet Summary API). Live CPU / memory come
from the kubelet Summary API (``nodes/proxy``) because the appliance ships no
metrics-server / Prometheus — same source the TTY console uses. See
``services/appliance/cluster_health.py`` for the gather.
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
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"kubeapi unreachable from api: {exc}",
        )
    await _merge_host_partitions(db, snap)
    return ClusterHealth(**snap)


@router.get(
    "/health/stream",
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
                snap = cluster_unavailable(f"kubeapi unreachable: {exc}")
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
