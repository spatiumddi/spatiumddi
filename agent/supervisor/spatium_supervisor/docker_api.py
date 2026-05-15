"""Direct HTTP-over-unix-socket client for the docker daemon API.

Replaces the ``subprocess.run(["docker", "..."])`` pattern used in
``heartbeat._docker_image_present`` + ``service_lifecycle.
_running_supervised_services``. The CLI shells the supervisor was
firing on every heartbeat (3× ``docker images`` for capability
probes + ``docker compose ps`` for running-service check) cost
~300 ms each on a 1-CPU appliance VM purely in Go binary startup +
arg parsing + JSON re-formatting — direct API calls return in
~10 ms. On a fleet with 60-second heartbeats that's 30× less CPU
spent on what is essentially read-only state inspection.

``docker compose up -d`` / ``stop`` still go through the CLI in
``service_lifecycle`` — compose orchestration is a CLI-plugin
concern, not a daemon-API one, and the heartbeat-skip optimisation
(env-file-hash sidecar) means those subprocess calls only fire on
actual role transitions, not every tick.

Failure mode is the same as the old subprocess path: any error
(socket missing, EACCES, timeout, malformed response) returns an
empty result + a structlog warning. Callers treat that the same as
"no images / no containers", which is correct for the supervisor's
capability + lifecycle paths.
"""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_DOCKER_SOCK = "/var/run/docker.sock"
# Pinned API version — every docker engine since 23.x supports 1.45.
# Pinning insulates the supervisor from server-side default-version
# drift between docker engine releases on the appliance host.
_API_VERSION = "1.45"


class _UnixHTTPConnection(http.client.HTTPConnection):
    """``http.client.HTTPConnection`` subclass that dials a unix
    socket instead of a TCP address. Lets us reuse stdlib's HTTP/1.1
    parser (handles chunked encoding, content-length, etc.) without
    pulling in requests-unixsocket or httpx-transport gymnastics."""

    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def _docker_get(path: str, timeout: float = 10.0) -> Any:
    """GET ``path`` from /var/run/docker.sock and return parsed JSON.

    Raises ``RuntimeError`` on non-2xx + on any I/O / parse failure.
    Callers wrap in try/except to convert to a graceful empty result.
    """
    conn = _UnixHTTPConnection(_DOCKER_SOCK, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(
                f"docker api {path}: status {resp.status} body {body[:200]!r}"
            )
        return json.loads(body)
    finally:
        conn.close()


def list_image_repos(timeout: float = 10.0) -> set[str]:
    """Return the set of every repository name (any tag) loaded into
    the docker daemon. Matches what ``docker images --format
    '{{.Repository}}'`` returns, minus duplicates from multiple tags
    of the same repo.

    Empty set on any error (daemon down, EACCES on socket, etc.) —
    the supervisor's capability path treats that as "no images" which
    is the safe default (role checkboxes stay disabled until the
    supervisor can prove the image is loaded).
    """
    try:
        data = _docker_get(f"/v{_API_VERSION}/images/json", timeout=timeout)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        log.warning("supervisor.docker_api.list_images_failed", error=str(exc))
        return set()
    repos: set[str] = set()
    for img in data:
        for tag in img.get("RepoTags") or []:
            if not tag or tag == "<none>:<none>":
                continue
            # tag is "repo:tagname"; strip the trailing :tag
            repo = tag.rsplit(":", 1)[0]
            if repo:
                repos.add(repo)
    return repos


def list_running_containers(timeout: float = 10.0) -> list[dict[str, Any]]:
    """Return the list of currently-running containers (``all=0``).
    Each dict is the raw shape the docker engine returns — callers
    pull ``Labels`` for the compose-service identifier + ``State``
    for the running-status check.

    Returns ``[]`` on any error — same fail-soft semantics as
    ``list_image_repos``.
    """
    try:
        return _docker_get(f"/v{_API_VERSION}/containers/json", timeout=timeout)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        log.warning("supervisor.docker_api.list_containers_failed", error=str(exc))
        return []


__all__ = [
    "list_image_repos",
    "list_running_containers",
]
