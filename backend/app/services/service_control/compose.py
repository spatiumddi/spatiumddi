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


def _self_id() -> str:
    """Our own container id.

    Docker sets the container hostname to the short container id unless
    the operator overrode it, and the Docker API accepts a short id or a
    name wherever it accepts an id — so this resolves either way.
    """
    return os.environ.get("HOSTNAME", "").strip()


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
    ident = _self_id()
    if not ident:
        return None
    try:
        async with _client() as client:
            resp = await client.get(f"/containers/{ident}/json")
            if resp.status_code != 200:
                logger.debug("compose_self_inspect_status", status=resp.status_code)
                return None
            payload = resp.json() or {}
            labels = (payload.get("Config") or {}).get("Labels") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("compose_self_inspect_failed", error=str(exc))
        return None
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
    async with _client() as client:
        resp = await client.get("/containers/json", params={"all": "true", "filters": filters})
        if resp.status_code != 200:
            raise ComposeUnavailableError(f"docker returned {resp.status_code} listing containers")
        rows: list[dict[str, Any]] = resp.json()
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
    async with _client() as client:
        resp = await client.post(f"/containers/{container_id}/{action}")
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
