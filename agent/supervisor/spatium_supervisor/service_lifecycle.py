"""Docker-compose lifecycle for the supervisor (#170 Wave D follow-up).

Pairs with ``role_orchestrator.py``: that module computes the
target ``COMPOSE_PROFILES`` + env file from the control plane's
role assignment; this one actually runs ``docker compose`` against
that env file so the service containers come up / go down.

The appliance compose file at
``/etc/spatiumddi/docker-compose.yml`` (managed by the appliance
ISO; not the supervisor's to write) carries every service the
supervisor can ever start. ``COMPOSE_PROFILES`` decides which subset
runs — the supervisor flips it via the env file every heartbeat.

Failure semantics:

* Stop / start failures don't crash the supervisor — they bubble up
  as a ``LifecycleResult`` with ``ok=False`` and a short reason
  string. The supervisor reports the reason in the next heartbeat's
  ``role_switch_state`` field so the Fleet UI can render a red chip.
* No automatic revert in this commit. The operator sees the failure
  in the UI + can either fix the underlying issue (image missing,
  port conflict — see Phase E2 pre-flight) and let the next
  heartbeat retry, or reassign the role.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import docker_api

# Default compose file on the appliance — the ISO drops it under
# /usr/local/share/spatiumddi/. (Earlier comments wrongly said
# /etc/spatiumddi/; the actual install path is /usr/local/share/.)
# Operators on docker / k8s deployments don't run this lifecycle
# path; ``apply_role_assignment`` short-circuits when the compose
# file is missing.
_DEFAULT_COMPOSE_FILE = Path("/usr/local/share/spatiumddi/docker-compose.yml")

# #170 Wave D follow-up — the appliance host's main env file (the
# one ``spatiumddi-firstboot`` populates from install-wizard input)
# is bind-mounted into the supervisor at ``/etc/spatiumddi-host/.env``
# (see appliance compose: ``/etc/spatiumddi:/etc/spatiumddi-host:ro``).
# Passing it as an additional ``--env-file`` ahead of the role env
# file gives ``docker compose`` access to operator-set knobs like
# ``DOCKER_GID`` / ``DNS_AGENT_KEY`` / ``HTTP_PORT`` without the
# supervisor having to re-emit each one into the role env. Later
# ``--env-file`` arguments override earlier matching keys, so the
# role env file's role-scoped vars (``COMPOSE_PROFILES``, etc.) still
# win on collision.
_HOST_ENV_FILE = Path("/etc/spatiumddi-host/.env")

# Every service the supervisor can start. Names match the
# ``services:`` keys in the appliance compose. The active subset
# = intersection of this list + the operator's role assignment.
SUPERVISED_SERVICES = ("dns-bind9", "dns-powerdns", "dhcp-kea")


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of one ``apply_role_assignment`` pass.

    ``state`` mirrors what the supervisor reports in the next
    heartbeat under ``role_switch_state``: ``idle`` / ``ready`` /
    ``failed``. ``reason`` carries the failure detail (compose
    stderr first line is usually enough) so the operator can
    triage without SSH-ing in.
    """

    state: str  # ready | failed | idle
    reason: str | None = None
    started: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()


def _compose_available(compose_file: Path) -> tuple[bool, str | None]:
    """Return ``(available, reason)``. False on dev (no compose file)
    or on a host that doesn't have docker installed — the supervisor
    short-circuits cleanly in either case."""
    if not compose_file.exists():
        return False, f"compose file missing: {compose_file}"
    if shutil.which("docker") is None:
        return False, "docker binary not on PATH"
    return True, None


def _running_supervised_services(compose_file: Path) -> list[str]:
    """Return the subset of :data:`SUPERVISED_SERVICES` currently in
    running state. Best-effort — a docker daemon failure returns an
    empty list (caller starts everything in the desired set;
    redundant ``up -d`` on an already-running service is a no-op).

    Reads /var/run/docker.sock directly via ``docker_api`` instead of
    shelling out to ``docker compose ps`` — same data, ~30× faster
    (no compose CLI fork/exec + JSON re-serialisation). The compose
    project name is stamped onto every container via the
    ``com.docker.compose.project`` label by ``docker compose up``;
    filter on that + the ``com.docker.compose.service`` label to
    pick out the supervised services without ambiguity.
    """
    # The compose project name defaults to the compose file's parent
    # directory name lowercased — for our appliance install that's
    # ``spatiumddi`` (the file lives in /usr/local/share/spatiumddi/).
    # Keying on the project label means an operator who manually
    # ``docker run``s an arbitrary container named ``dns-bind9-test``
    # won't false-positive into the running set.
    expected_project = compose_file.parent.name
    running: list[str] = []
    for c in docker_api.list_running_containers():
        labels = c.get("Labels") or {}
        if labels.get("com.docker.compose.project") != expected_project:
            continue
        service = labels.get("com.docker.compose.service")
        # State is "running" for actively-running containers; docker
        # API uses "State" (top-level) for the engine-level state,
        # which only returns running by default when ``all=0``.
        if service in SUPERVISED_SERVICES:
            running.append(service)
    return running


def apply_role_assignment(
    profiles: list[str],
    env_file: Path,
    *,
    compose_file: Path = _DEFAULT_COMPOSE_FILE,
) -> LifecycleResult:
    """Bring the appliance's service containers in line with the
    ``profiles`` list.

    Algorithm:

    1. If compose isn't available (dev / docker / k8s deploys), return
       ``idle`` immediately — the supervisor reports that to the
       control plane so the Fleet UI shows the appliance as paired
       but not running anything yet.
    2. Resolve the desired service set = intersection of profiles +
       :data:`SUPERVISED_SERVICES`.
    3. ``docker compose stop`` any supervised service currently
       running that's not in the desired set. Removes service
       containers cleanly; their state volume survives (named
       volume).
    4. ``docker compose --env-file=<env_file> up -d <desired>`` to
       start the desired set. Idempotent — already-running services
       are no-ops.
    5. Returns ``ready`` on success, ``failed`` (with the first
       stderr line) on any compose error.
    """
    available, reason = _compose_available(compose_file)
    if not available:
        return LifecycleResult(state="idle", reason=reason)

    desired = [p for p in profiles if p in SUPERVISED_SERVICES]
    running = _running_supervised_services(compose_file)
    to_stop = [s for s in running if s not in desired]
    to_start = desired  # ``up -d`` is idempotent — pass the full target set

    stopped: list[str] = []
    started: list[str] = []

    if to_stop:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "stop",
                    *to_stop,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return LifecycleResult(state="failed", reason=f"stop: {exc}")
        if result.returncode != 0:
            first = result.stderr.strip().splitlines()[:1]
            return LifecycleResult(
                state="failed",
                reason=f"stop failed: {(first[0] if first else 'no stderr')}",
            )
        stopped = list(to_stop)

    if to_start:
        try:
            cmd = ["docker", "compose", "-f", str(compose_file)]
            if _HOST_ENV_FILE.exists():
                cmd += ["--env-file", str(_HOST_ENV_FILE)]
            cmd += ["--env-file", str(env_file), "up", "-d", *to_start]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return LifecycleResult(state="failed", reason=f"up: {exc}")
        if result.returncode != 0:
            first = result.stderr.strip().splitlines()[:1]
            return LifecycleResult(
                state="failed",
                reason=f"up failed: {(first[0] if first else 'no stderr')}",
            )
        started = list(to_start)

    if not desired and not stopped and not started:
        return LifecycleResult(state="idle")
    return LifecycleResult(
        state="ready",
        started=tuple(started),
        stopped=tuple(stopped),
    )


def lifecycle_state_for_assignment(role_assignment: dict[str, Any] | None) -> str:
    """Helper for the heartbeat-skip case: when the supervisor has
    a role assignment but compose isn't available, we still need to
    report a sensible ``role_switch_state``. ``idle`` works for both
    "nothing assigned" and "can't run anything here" — the Fleet UI
    renders the deployment_kind chip alongside, so the operator can
    tell them apart."""
    role_assignment = role_assignment or {}
    roles = list(role_assignment.get("roles") or [])
    if not any(r in SUPERVISED_SERVICES for r in roles):
        return "idle"
    return "idle"


__all__ = [
    "LifecycleResult",
    "SUPERVISED_SERVICES",
    "apply_role_assignment",
    "lifecycle_state_for_assignment",
]
