"""Service container watchdog (#170 Wave E).

Bridges the gap between ``role_switch_state`` (set once when
``apply_role_assignment`` runs) and the *current* runtime state of
the supervisor-managed service containers. Without this watchdog, a
container that crashed / got removed / had its image purged between
heartbeats would leave the supervisor reporting a stale
``role_switch_state=ready`` indefinitely — ``apply_role_assignment``
no-ops on every subsequent heartbeat because the env-hash skip
short-circuits when nothing in the desired set changed.

Algorithm:

1. Read the assigned compose profiles from the supervisor's
   ``role-compose.env`` (the file the supervisor itself writes on
   every role-assignment apply). Maps profile → compose service
   (dhcp profile → dhcp-kea service; everything else is identity).
2. Snapshot the running containers via ``docker_api`` — same
   ``/var/run/docker.sock`` HTTP call the heartbeat uses, so the
   watchdog adds zero subprocess overhead on the 1-CPU appliance VM.
3. For each desired service, derive a ``status`` verdict:
   * ``missing`` — no container in the running set.
   * ``starting`` — engine reports ``(health: starting)``.
   * ``unhealthy`` — engine reports ``(unhealthy)``, exited, or
     restart-looping.
   * ``healthy`` — running + healthcheck passing (or no healthcheck
     declared).
4. Track when each (service, status) tuple was first observed in a
   process-local history map. The supervisor reports ``since`` as
   the ISO-8601 of the first-observed timestamp, so the Fleet UI
   can show "missing for 3 m 24 s" rather than just a static
   verdict. History resets on supervisor restart — acceptable, the
   watchdog re-observes within one cadence.
5. Optionally auto-heal: when one or more services are ``missing``,
   re-fire ``apply_role_assignment`` with the current profile set.
   ``up -d`` is idempotent, so present-and-healthy services no-op
   and only the missing ones come up.

The heartbeat decides cadence — typically every 5 minutes (5
heartbeats at the default 60 s interval). Steady-state cost is one
docker_api call + dict bookkeeping; ~10 ms total.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from . import docker_api
from .service_lifecycle import apply_role_assignment

log = structlog.get_logger(__name__)


# Compose profile → compose service name. ``COMPOSE_PROFILES`` in
# role-compose.env carries profile values; container labels use the
# service name. The two are identical for DNS but DHCP's profile is
# ``dhcp`` while its service is ``dhcp-kea``.
_PROFILE_TO_SERVICE: dict[str, str] = {
    "dns-bind9": "dns-bind9",
    "dns-powerdns": "dns-powerdns",
    "dhcp": "dhcp-kea",
}


@dataclass(frozen=True)
class ServiceHealth:
    """Per-service watchdog verdict.

    ``status`` ∈ {healthy, missing, unhealthy, starting}. The Fleet
    drilldown renders a green chip on healthy, amber on starting,
    rose on unhealthy / missing. ``since_ts`` is the unix-epoch
    seconds of the first observation in this status; the heartbeat
    serialises it as an ISO-8601 wall-clock string for the API.
    """

    service: str
    role: str
    status: str
    since_ts: float
    container_id: str | None


# Process-local history of (status, monotonic-first-seen). Survives
# heartbeat ticks within one supervisor process; resets on restart
# (acceptable — first watchdog tick after restart re-observes).
_status_history: dict[str, tuple[str, float]] = {}


def read_assigned_profiles(env_file: Path) -> list[str]:
    """Parse the ``COMPOSE_PROFILES`` value from role-compose.env.

    Returns ``[]`` on missing file, empty value, or read error. The
    supervisor writes this file on every heartbeat that returns a
    role_assignment, so a non-empty result means the supervisor has
    been told about roles + the operator hasn't cleared them.
    """
    if not env_file.exists():
        return []
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("COMPOSE_PROFILES="):
                val = line.split("=", 1)[1].strip()
                if not val:
                    return []
                return [p.strip() for p in val.split(",") if p.strip()]
    except OSError:
        pass
    return []


def _derive_status(container: dict[str, Any]) -> str:
    """Translate the engine API's container state + status fields
    into the watchdog's compact verdict."""
    state = (container.get("State") or "").lower()
    api_status = container.get("Status") or ""
    if state != "running":
        return "unhealthy"
    # Status string carries the healthcheck verdict in parens —
    # "Up 5 minutes (healthy)" / "Up 4 seconds (health: starting)" /
    # "Up 1 hour (unhealthy)" / "Up 10 days" (no healthcheck).
    if "(unhealthy)" in api_status:
        return "unhealthy"
    if "(health: starting)" in api_status:
        return "starting"
    if "(healthy)" in api_status:
        return "healthy"
    # Running, no healthcheck declared → treat as healthy. Pre-#170
    # service compose entries declared healthchecks on every container,
    # but operator-pasted custom overrides might not.
    return "healthy"


def check_health(
    env_file: Path,
    *,
    auto_heal: bool = True,
) -> dict[str, dict[str, Any]]:
    """One watchdog pass.

    Returns a JSON-serialisable ``role_health`` dict keyed by compose
    service name. Empty dict when no roles are assigned (idle
    appliance — nothing to watch). ``auto_heal=False`` lets tests
    drive the watcher without re-firing the compose subprocess.
    """
    profiles = read_assigned_profiles(env_file)
    desired_services: dict[str, str] = {}  # service → role(profile)
    for p in profiles:
        svc = _PROFILE_TO_SERVICE.get(p)
        if svc is not None:
            desired_services[svc] = p

    if not desired_services:
        # Garbage-collect history so a later role-assign starts fresh.
        _status_history.clear()
        return {}

    by_service: dict[str, dict[str, Any]] = {}
    for c in docker_api.list_running_containers():
        labels = c.get("Labels") or {}
        svc = labels.get("com.docker.compose.service")
        if svc in desired_services:
            by_service[svc] = c

    out: dict[str, dict[str, Any]] = {}
    missing_services: list[str] = []
    now_wall = time.time()
    now_mono = time.monotonic()

    for svc, role in desired_services.items():
        container = by_service.get(svc)
        if container is None:
            status = "missing"
            container_id = None
            missing_services.append(svc)
        else:
            status = _derive_status(container)
            container_id = (container.get("Id") or "")[:12] or None

        prev = _status_history.get(svc)
        if prev is None or prev[0] != status:
            _status_history[svc] = (status, now_mono)
            since_wall = now_wall
        else:
            # Same status as last observation — preserve the original
            # first-seen wall-clock by deriving from the monotonic
            # delta. now_wall - (now_mono - prev_mono).
            since_wall = now_wall - (now_mono - prev[1])

        out[svc] = {
            "role": role,
            "status": status,
            "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_wall)),
            "container_id": container_id,
        }

    # Garbage-collect stale entries (operator switched roles).
    for stale in list(_status_history):
        if stale not in desired_services:
            del _status_history[stale]

    # Auto-heal: kick missing services. ``apply_role_assignment``
    # diffs running vs desired; healthy services no-op, missing ones
    # come up. Bypass the env-hash skip cache that heartbeat-level
    # apply uses — the env file is unchanged but the runtime diverged.
    if auto_heal and missing_services:
        log.warning(
            "supervisor.watchdog.missing_services",
            missing=missing_services,
        )
        result = apply_role_assignment(profiles, env_file)
        log.info(
            "supervisor.watchdog.heal_attempted",
            state=result.state,
            reason=result.reason,
            started=list(result.started),
        )

    return out


__all__ = [
    "ServiceHealth",
    "check_health",
    "read_assigned_profiles",
]
