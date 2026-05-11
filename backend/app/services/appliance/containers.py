"""Docker container listing + control + log tailing (Phase 4d).

Talks to the docker socket via the docker SDK; gated on
``settings.appliance_mode`` + the socket actually being reachable
(group_add for the docker gid is required on the appliance compose;
see docker-compose.yml).

The endpoints in ``app/api/v1/appliance/containers.py`` wrap these
helpers + add permission gates + audit-log rows.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anyio
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_DOCKER_SOCK = Path("/var/run/docker.sock")
# Spatium containers carry this label-prefix on the compose ``name``
# (e.g. ``spatiumddi-api-1``, ``spatiumddi-frontend``). The listing
# endpoint filters to these so operators don't see unrelated stuff
# (other compose projects, system containers) on the same host.
_SPATIUM_NAME_PREFIX = "spatiumddi-"


class DockerUnavailableError(RuntimeError):
    """Raised when the docker socket can't be reached or the SDK is
    missing. The router translates these into 503 Service Unavailable."""


@dataclass
class ContainerSummary:
    name: str
    image: str
    state: str  # running / restarting / exited / paused / created
    status: str  # human-readable e.g. "Up 5 minutes (healthy)"
    health: str | None  # healthy / unhealthy / starting / None
    short_id: str
    started_at: datetime | None
    is_spatium: bool


def _client():
    """Lazy docker SDK import + connect.

    Defer the import so non-appliance deploys don't pay the (small)
    import cost. Raises DockerUnavailableError on any failure.
    """
    if not _DOCKER_SOCK.exists():
        raise DockerUnavailableError("docker socket not mounted into the api container")
    try:
        import docker  # noqa: PLC0415
        from docker.errors import DockerException  # noqa: PLC0415
    except ImportError as exc:
        raise DockerUnavailableError(f"docker SDK not installed: {exc}") from exc
    try:
        return docker.from_env()
    except DockerException as exc:
        raise DockerUnavailableError(f"docker client init failed: {exc}") from exc


def list_containers() -> list[ContainerSummary]:
    if not settings.appliance_mode:
        return []
    client = _client()
    out: list[ContainerSummary] = []
    for c in client.containers.list(all=True):
        attrs = c.attrs or {}
        state_obj = attrs.get("State", {}) or {}
        health_obj = state_obj.get("Health") or {}
        started_raw = state_obj.get("StartedAt")
        started_at: datetime | None
        try:
            # docker emits ISO 8601 in UTC; py3.11+ fromisoformat handles
            # the trailing Z directly in 3.12 but we strip just in case.
            started_at = (
                datetime.fromisoformat(started_raw.rstrip("Z") + "+00:00")
                if started_raw and started_raw != "0001-01-01T00:00:00Z"
                else None
            )
        except (ValueError, AttributeError):
            started_at = None
        name = c.name or ""
        out.append(
            ContainerSummary(
                name=name,
                image=(c.image.tags[0] if c.image and c.image.tags else c.attrs.get("Image", "")),
                state=state_obj.get("Status", "unknown"),
                status=c.status,
                health=health_obj.get("Status") if health_obj else None,
                short_id=c.short_id,
                started_at=started_at,
                is_spatium=name.startswith(_SPATIUM_NAME_PREFIX),
            )
        )
    # Spatium containers first, then alphabetical within each group.
    out.sort(key=lambda c: (not c.is_spatium, c.name))
    return out


def container_action(name: str, action: str) -> None:
    """Apply ``start`` / ``stop`` / ``restart`` to the named container."""
    valid = {"start", "stop", "restart"}
    if action not in valid:
        raise ValueError(f"unknown container action: {action!r}")
    client = _client()
    try:
        from docker.errors import APIError, NotFound  # noqa: PLC0415
    except ImportError as exc:
        raise DockerUnavailableError(str(exc)) from exc
    try:
        container = client.containers.get(name)
    except NotFound as exc:
        raise DockerUnavailableError(f"no container named {name!r}") from exc
    except APIError as exc:
        raise DockerUnavailableError(str(exc)) from exc
    getattr(container, action)()
    logger.info("appliance_container_action", name=name, action=action)


def get_container_logs(name: str, tail: int = 200, since_seconds: int | None = None) -> str:
    """Return the last ``tail`` log lines (or lines since N seconds ago).

    Combined stdout+stderr, decoded to UTF-8 with replacement for
    binary fragments. Truncated at ``tail`` lines on the server side
    so the response is bounded.
    """
    client = _client()
    try:
        container = client.containers.get(name)
    except Exception as exc:  # noqa: BLE001
        raise DockerUnavailableError(str(exc)) from exc
    kwargs: dict[str, object] = {
        "stdout": True,
        "stderr": True,
        "tail": tail,
        "timestamps": True,
    }
    if since_seconds is not None:
        from datetime import timedelta

        kwargs["since"] = datetime.now(UTC) - timedelta(seconds=since_seconds)
    raw = container.logs(**kwargs)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


async def stream_container_logs(name: str, tail: int = 100) -> AsyncGenerator[str, None]:
    """Yield log lines from the named container as they arrive.

    Wraps the docker SDK's blocking iterator with anyio.to_thread so
    the FastAPI event loop isn't blocked. Each yielded chunk is a
    UTF-8 string; the router formats them as SSE ``data:`` frames.

    Cancellation: when the SSE client disconnects, FastAPI cancels
    this generator; anyio.to_thread.run_sync runs to its next
    cancellation checkpoint and we exit cleanly.
    """
    client = _client()
    try:
        container = client.containers.get(name)
    except Exception as exc:  # noqa: BLE001
        raise DockerUnavailableError(str(exc)) from exc

    log_stream = container.logs(
        stdout=True,
        stderr=True,
        stream=True,
        follow=True,
        tail=tail,
        timestamps=True,
    )

    # ``log_stream`` is a sync byte-iterator. Pulling each chunk inside
    # a worker thread keeps the event loop responsive — important
    # because SSE clients might stay connected for hours and the
    # event loop has other things to do.
    iterator = iter(log_stream)

    def _next() -> bytes | None:
        try:
            return next(iterator)
        except StopIteration:
            return None

    try:
        while True:
            chunk = await anyio.to_thread.run_sync(_next)
            if chunk is None:
                break
            if isinstance(chunk, bytes):
                yield chunk.decode("utf-8", errors="replace")
            else:
                yield str(chunk)
    finally:
        # Best-effort close of the underlying urllib3 response so
        # docker SDK doesn't leave a dangling socket on disconnect.
        close = getattr(log_stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
