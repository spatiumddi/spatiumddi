"""Docker-compose lifecycle backend (issue #890).

Talks to the local Docker daemon over the UDS the operator mounts into
the api container — the same socket ``api/v1/admin/containers.py``
already reads stats from, so no new plumbing and no new privilege
beyond what the mount already grants.

**The allowlist is the compose project we are ourselves in.** The api
container reads its *own* labels (``com.docker.compose.project``) and
only ever acts on containers carrying the same value. That needs no
configuration, cannot be widened by a request, and fails closed: if we
cannot identify our own project — the api is not running under compose,
or the socket does not expose us — the backend reports unavailable
rather than falling back to a name prefix, which a co-tenant container
could match on purpose.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

DOCKER_SOCKET = "/var/run/docker.sock"

_PROJECT_LABEL = "com.docker.compose.project"
_SERVICE_LABEL = "com.docker.compose.service"


class ComposeUnavailableError(RuntimeError):
    """The Docker socket is absent, unreachable, or cannot identify us."""


def socket_available() -> bool:
    try:
        return Path(DOCKER_SOCKET).is_socket()
    except OSError:
        return False


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://docker",
        transport=httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET),
        timeout=15.0,
    )


# Docker bind-mounts /etc/hosts, /etc/hostname and /etc/resolv.conf from
# /var/lib/docker/containers/<full-id>/ into every container, so the id
# is readable out of our own mount table.
_MOUNTINFO = Path("/proc/self/mountinfo")
_CONTAINER_ID_RE = re.compile(r"/containers/([0-9a-f]{64})")


def _self_candidates() -> list[str]:
    """Identifiers to try against ``/containers/{id}/json``, best first.

    ``$HOSTNAME`` is NOT a reliable answer here. The Docker API resolves
    that path segment as an id or a *name*, and while Docker defaults a
    container's hostname to its own short id, compose lets a service set
    ``hostname:`` — and there the value is neither. Inspect then 404s,
    the capability collapses to ``none``, and the whole compose backend
    disappears with no explanation.

    So the mount table is tried first (it carries the real id regardless
    of hostname) and ``$HOSTNAME`` is the fallback for the environments
    where it isn't readable.
    """
    out: list[str] = []
    try:
        match = _CONTAINER_ID_RE.search(_MOUNTINFO.read_text(encoding="utf-8"))
        if match:
            out.append(match.group(1))
    except OSError as exc:
        logger.debug("compose_self_mountinfo_failed", error=str(exc))
    host = os.environ.get("HOSTNAME", "").strip()
    if host and host not in out:
        out.append(host)
    return out


@dataclass(frozen=True)
class SelfIdentity:
    """Who the api container is, per the daemon.

    ``container_id`` comes from the same inspect call as ``project``
    rather than being assumed equal to ``$HOSTNAME``: compose lets an
    operator set ``hostname:`` on a service, and where that happens the
    hostname is not the container id. Self-detection matters — a
    self-targeted action has to be deferred past the response or the
    daemon kills this process mid-reply.
    """

    project: str
    container_id: str


async def own_identity() -> SelfIdentity | None:
    """The compose project + container id of this api container.

    ``None`` means "cannot establish scope", and every caller treats that
    as unavailable rather than widening to all containers.
    """
    payload: dict[str, Any] | None = None
    try:
        async with _client() as client:
            for ident in _self_candidates():
                resp = await client.get(f"/containers/{ident}/json")
                if resp.status_code == 200:
                    payload = resp.json() or {}
                    break
                logger.debug("compose_self_inspect_status", ident=ident, status=resp.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("compose_self_inspect_failed", error=str(exc))
        return None
    if payload is None:
        return None
    labels = (payload.get("Config") or {}).get("Labels") or {}
    project = labels.get(_PROJECT_LABEL)
    if not project:
        return None
    return SelfIdentity(project=str(project), container_id=str(payload.get("Id") or "")[:12])


def _short_name(raw_names: list[str] | None) -> str:
    if not raw_names:
        return ""
    return str(raw_names[0]).lstrip("/")


async def list_project_containers(project: str) -> list[dict[str, Any]]:
    """Every container (running or not) in ``project``.

    Filtering is done by the daemon via the ``label`` filter rather than
    client-side, so a container that appears between the list and the
    action still has to carry the label to be actionable.
    """
    # json.dumps rather than string interpolation: a project name is
    # operator-chosen and can contain characters that would otherwise
    # break out of the filter document.
    filters = json.dumps({"label": [f"{_PROJECT_LABEL}={project}"]})
    # Transport failures are translated here rather than allowed to
    # escape: an httpx timeout reaching the caller as a raw exception
    # becomes an opaque 500 (and, on the self-targeted path, an
    # unhandled background-task error) instead of the 502 / reported
    # ``error`` the caller is written to surface.
    try:
        async with _client() as client:
            resp = await client.get("/containers/json", params={"all": "true", "filters": filters})
            if resp.status_code != 200:
                raise ComposeUnavailableError(
                    f"docker returned {resp.status_code} listing containers"
                )
            rows: list[dict[str, Any]] = resp.json()
    except httpx.HTTPError as exc:
        raise ComposeUnavailableError(f"{type(exc).__name__}: {exc}") from exc
    except ValueError as exc:
        raise ComposeUnavailableError(f"unparseable docker response: {exc}") from exc
    return rows


def summarise(row: dict[str, Any]) -> dict[str, Any]:
    labels = row.get("Labels") or {}
    return {
        "service": str(labels.get(_SERVICE_LABEL) or ""),
        "container_name": _short_name(row.get("Names")),
        "container_id": str(row.get("Id", ""))[:12],
        "image": str(row.get("Image") or ""),
        "state": str(row.get("State") or "unknown"),
        "status": str(row.get("Status") or ""),
    }


async def act(container_id: str, action: str) -> None:
    """POST ``/containers/{id}/{action}`` and raise on anything but success.

    ``container_id`` is always a full id taken from a fresh inventory
    listing, never a caller-supplied string.
    """
    try:
        async with _client() as client:
            resp = await client.post(f"/containers/{container_id}/{action}")
    except httpx.HTTPError as exc:
        raise ComposeUnavailableError(f"{type(exc).__name__}: {exc}") from exc
    # 204 = done, 304 = already in the requested state (start on a running
    # container). Both are the outcome the operator asked for.
    if resp.status_code in (204, 304):
        return
    detail = resp.text.strip()[:300]
    raise ComposeUnavailableError(f"docker {action} returned {resp.status_code}: {detail}")


__all__ = [
    "DOCKER_SOCKET",
    "ComposeUnavailableError",
    "SelfIdentity",
    "act",
    "list_project_containers",
    "own_identity",
    "socket_available",
    "summarise",
]
