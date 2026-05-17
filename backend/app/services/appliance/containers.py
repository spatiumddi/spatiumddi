"""Pod listing + control + log tailing for the appliance Fleet UI.

Phase 11 wave 4 (#183) rewrite. Pre-Phase-11 this talked to the
docker socket via the docker SDK; appliance mode is now k3s-only
(see Phase 7 docker-strip), so we go through kubeapi instead via
the api pod's mounted ServiceAccount token.

The endpoints in ``app/api/v1/appliance/containers.py`` wrap
these helpers + add permission gates + audit-log rows. The public
exception name + dataclass shape are unchanged so callers don't
have to follow the rewrite.

Gated on ``settings.appliance_mode`` + the ServiceAccount actually
being mounted (``services/appliance/k8s.get_config()``). On docker-
compose / non-k8s deployments the gate fails fast + the endpoints
log + 503 — same shape the docker path used.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio
import structlog

from app.config import settings
from app.services.appliance import k8s

logger = structlog.get_logger(__name__)

# Pods carrying any of these labels show up as "spatium" rows (first
# in the listing, dot/marker styling client-side). The umbrella
# chart stamps every spatium pod with ``app.kubernetes.io/part-of=
# spatiumddi`` and the appliance chart stamps the same plus
# ``app.kubernetes.io/component=<role>``. Keep the surface low-
# touch: any matching label = spatium-owned.
_SPATIUM_PART_OF = "spatiumddi"


class DockerUnavailableError(RuntimeError):
    """Raised when kubeapi can't be reached or the ServiceAccount
    isn't mounted.

    Name kept for backwards compat with the router which translates
    this to 503 Service Unavailable. The error message body
    distinguishes the real cause for the operator's log.
    """


@dataclass
class ContainerSummary:
    """Pod summary in a docker-shape (name/image/state/status/
    health/short_id/started_at/is_spatium) for the existing
    ``/appliance/containers`` API surface.

    ``short_id`` is the pod's metadata.uid truncated to 12 chars
    (matches docker short_id length). ``health`` reflects the
    pod-level readiness summary (healthy / unhealthy / starting)
    derived from container statuses + waiting reasons — same
    vocabulary the docker container model used.
    """

    name: str
    image: str
    state: str  # running / pending / succeeded / failed / unknown
    status: str  # human-readable e.g. "Running (5m, healthy)"
    health: str | None  # healthy / unhealthy / starting / None
    short_id: str
    started_at: datetime | None
    is_spatium: bool


def _parse_pod_to_summary(pod: dict[str, Any]) -> ContainerSummary:
    """Flatten a kubeapi Pod object to ContainerSummary."""
    meta = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status_obj = pod.get("status") or {}
    name = meta.get("name") or ""
    labels = meta.get("labels") or {}
    part_of = labels.get("app.kubernetes.io/part-of") or ""

    containers = spec.get("containers") or []
    first_image = containers[0].get("image", "") if containers else ""

    phase = status_obj.get("phase") or "Unknown"
    # Health derived from container readiness + waiting reasons.
    container_statuses = status_obj.get("containerStatuses") or []
    health: str | None = None
    waiting_reason = ""
    if phase == "Running" and container_statuses:
        all_ready = all(cs.get("ready") for cs in container_statuses)
        if all_ready:
            health = "healthy"
        else:
            for cs in container_statuses:
                waiting = (cs.get("state") or {}).get("waiting") or {}
                r = waiting.get("reason") or ""
                if r:
                    waiting_reason = r
                    break
            if waiting_reason.lower() in (
                "crashloopbackoff",
                "errimagepull",
                "imagepullbackoff",
            ):
                health = "unhealthy"
            else:
                health = "starting"
    elif phase == "Pending":
        health = "starting"
    elif phase in ("Failed", "Unknown"):
        health = "unhealthy"

    # State mirrors the docker vocabulary roughly:
    #   Running   → running
    #   Pending   → starting (no docker analog; closest is "created")
    #   Succeeded → exited
    #   Failed    → exited
    #   Unknown   → unknown
    state = {
        "Running": "running",
        "Pending": "starting",
        "Succeeded": "exited",
        "Failed": "exited",
    }.get(phase, "unknown")

    # Human-readable status combining phase + age + health hint.
    # Same shape ``docker ps`` produces:  "Up 5 minutes (healthy)".
    start_time = status_obj.get("startTime")
    started_at: datetime | None = None
    if start_time:
        try:
            started_at = datetime.fromisoformat(
                start_time.rstrip("Z") + "+00:00"
            )
        except ValueError:
            started_at = None
    if waiting_reason:
        status_str = f"{phase} ({waiting_reason})"
    elif health:
        status_str = f"{phase} ({health})"
    else:
        status_str = phase

    return ContainerSummary(
        name=name,
        image=first_image,
        state=state,
        status=status_str,
        health=health,
        short_id=(meta.get("uid") or "")[:12],
        started_at=started_at,
        is_spatium=(part_of == _SPATIUM_PART_OF),
    )


def list_containers() -> list[ContainerSummary]:
    if not settings.appliance_mode:
        return []
    try:
        pods = k8s.list_pods()
    except k8s.KubeapiUnavailableError as exc:
        raise DockerUnavailableError(str(exc)) from exc
    out = [_parse_pod_to_summary(p) for p in pods]
    # Spatium pods first, then alphabetical within each group.
    out.sort(key=lambda c: (not c.is_spatium, c.name))
    return out


def container_action(name: str, action: str) -> None:
    """Apply ``start`` / ``stop`` / ``restart`` to the named pod.

    K8s vocabulary:
      * ``restart`` → delete the pod; the owning Deployment /
        DaemonSet recreates it on the next reconcile.
      * ``start`` / ``stop`` → not directly supported on a single
        pod (you'd scale the owning Deployment). For the appliance
        Fleet UI's purposes ``restart`` is the only meaningful
        action; ``start`` / ``stop`` raise ValueError (was a
        wishlist UX in the docker era).
    """
    if action == "restart":
        ok, err = k8s.delete_pod(name)
        if not ok:
            raise DockerUnavailableError(err or f"failed to delete pod {name}")
        logger.info("appliance_pod_restart", name=name)
        return
    if action in ("start", "stop"):
        raise ValueError(
            f"pod action {action!r} requires scaling the owning Deployment; "
            "use kubectl scale for now (UI exposure pending)"
        )
    raise ValueError(f"unknown pod action: {action!r}")


def get_container_logs(
    name: str, tail: int = 200, since_seconds: int | None = None
) -> str:
    """Return the last ``tail`` log lines (or lines since N seconds ago).

    Decoded UTF-8 with replacement for binary fragments. Truncated at
    ``tail`` lines server-side so response is bounded.
    """
    try:
        body = k8s.get_pod_logs(name, tail=tail, since_seconds=since_seconds)
    except k8s.KubeapiUnavailableError as exc:
        raise DockerUnavailableError(str(exc)) from exc
    return body


async def stream_container_logs(name: str, tail: int = 100) -> AsyncGenerator[str, None]:
    """Yield log lines from the named pod as they arrive.

    Wraps ``k8s.stream_pod_logs`` (a sync generator) with
    ``anyio.to_thread.run_sync`` so the FastAPI event loop isn't
    blocked. Each yielded chunk is a UTF-8 string with the
    trailing newline stripped; the router formats them as SSE
    ``data:`` frames.

    Cancellation: when the SSE client disconnects, FastAPI cancels
    this generator; the underlying http.client connection in the
    worker thread is closed via the generator's GeneratorExit
    propagation through the iter wrapper below.
    """
    try:
        gen = k8s.stream_pod_logs(name, tail=tail)
    except k8s.KubeapiUnavailableError as exc:
        raise DockerUnavailableError(str(exc)) from exc

    def _next() -> str | None:
        try:
            return next(gen)
        except StopIteration:
            return None

    try:
        while True:
            line = await anyio.to_thread.run_sync(_next)
            if line is None:
                break
            yield line
    finally:
        close = getattr(gen, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "ContainerSummary",
    "DockerUnavailableError",
    "container_action",
    "get_container_logs",
    "list_containers",
    "stream_container_logs",
]


_ = UTC  # silence "imported but unused" — kept for stable import line
