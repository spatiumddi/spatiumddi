"""Service container watchdog (#170 Wave E, #183 Phase 7).

Bridges the gap between ``role_switch_state`` (set once when
``apply_role_assignment`` runs) and the *current* runtime state of
the supervisor-managed service pods. Without this watchdog, a pod
that crashed / got removed / had its image purged between heartbeats
would leave the supervisor reporting a stale ``role_switch_state=
ready`` indefinitely — ``apply_role_assignment`` no-ops on every
subsequent heartbeat because the chart-content hash hasn't moved.

Phase 7 retired the docker-compose path; this watchdog is k3s-only.
Pre-Phase-7 the module branched on ``detect_runtime()`` to choose
between a ``docker_api.list_running_containers()`` poll and a
``k8s_api.list_pods()`` poll. Now it's just the kubeapi path.

Algorithm:

1. Read the assigned profiles from the supervisor's
   ``role-compose.env`` (the file the supervisor itself writes on
   every role-assignment apply). Map profile → chart-component name
   (dhcp profile → dhcp-kea, etc.).
2. Probe kubeapi ``/readyz``. If kubeapi is wedged, mark every
   desired service ``missing`` so the Fleet UI shows the cluster-
   level fault loud + skip the rest of the probe.
3. Enumerate pods in ``spatium`` via the chart's
   ``app.kubernetes.io/part-of=spatiumddi`` selector. For each
   desired service, derive a ``status`` verdict from the matching
   Pod's phase + container statuses:
   * ``missing`` — no pod found.
   * ``starting`` — Pending or Running-but-not-ready.
   * ``unhealthy`` — CrashLoopBackOff / ErrImagePull / Failed.
   * ``healthy`` — Running + all containers ready.
4. Track first-observed timestamps per (service, status) in a
   process-local history map. ``since`` rides every heartbeat.
5. Optionally auto-heal: when one or more services are ``missing``,
   re-fire ``apply_role_assignment`` (PATCH the HelmChart CR). helm-
   controller diffs internally — present-and-healthy services
   no-op, missing ones come up.

Cadence: 5 min (5 heartbeats at the default 60 s interval). Steady-
state cost is one ``GET /api/v1/namespaces/spatium/pods`` + dict
bookkeeping; sub-100 ms.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from . import k8s_api
from .service_lifecycle import apply_role_assignment

log = structlog.get_logger(__name__)


# Compose-profile-name → chart-component name. ``COMPOSE_PROFILES``
# in role-compose.env carries profile values; pod labels carry the
# chart component. ``dhcp`` profile maps to ``dhcp-kea`` component;
# everything else is identity.
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


def _derive_pod_status(pod: k8s_api.PodStatus) -> str:
    """Translate a Pod's phase + containerStatuses into the four-
    value verdict the Fleet drilldown renders (healthy / starting /
    unhealthy / missing).
    """
    phase = pod.status
    if phase == "Pending":
        return "starting"
    if phase in ("Failed", "Succeeded"):
        # Succeeded for a long-running service container means the
        # process exited cleanly but the workload should be Running.
        return "unhealthy"
    if phase != "Running":
        return "starting"
    # Pod is Running — check each container's Ready bit. Any
    # not-ready (CrashLoopBackOff, ImagePullBackOff, etc) demotes
    # the whole verdict to unhealthy. Empty container list = pod
    # mid-init.
    if not pod.container_statuses:
        return "starting"
    for cs in pod.container_statuses:
        if not cs.get("ready"):
            waiting = cs.get("state", {}).get("waiting", {})
            reason = (waiting.get("reason") or "").lower() if waiting else ""
            if reason in ("crashloopbackoff", "errimagepull", "imagepullbackoff"):
                return "unhealthy"
            return "starting"
    return "healthy"


def _check_health_k3s(
    env_file: Path,
    desired_services: dict[str, str],
    *,
    auto_heal: bool,
) -> dict[str, dict[str, Any]]:
    """One k3s health-check pass.

    Probes ``/readyz`` first — if kubeapi itself is wedged, mark
    every desired service as ``missing`` so the Fleet UI carries a
    loud red banner and the operator knows the cluster (not the
    workload) is the problem. Then enumerates pods in the
    ``spatium`` namespace via the kubeapi label selector
    ``app.kubernetes.io/part-of=spatiumddi`` and matches against the
    ``app.kubernetes.io/component`` label.
    """
    out: dict[str, dict[str, Any]] = {}
    now_wall = time.time()
    now_mono = time.monotonic()

    # kubeapi reachability is the precondition — if it's down, every
    # service is effectively missing AND auto-heal can't do anything
    # useful (HelmChart PATCH would fail too). Surface this loud.
    if not k8s_api.check_kubeapi_ready():
        log.error("supervisor.watchdog.kubeapi_unreachable")
        for svc, role in desired_services.items():
            prev = _status_history.get(svc)
            if prev is None or prev[0] != "missing":
                _status_history[svc] = ("missing", now_mono)
                since_wall = now_wall
            else:
                since_wall = now_wall - (now_mono - prev[1])
            out[svc] = {
                "role": role,
                "status": "missing",
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_wall)),
                "container_id": None,
            }
        return out

    pods = k8s_api.list_pods(
        namespace="spatium",
        label_selector="app.kubernetes.io/part-of=spatiumddi",
    )
    by_service: dict[str, k8s_api.PodStatus] = {}
    for pod in pods:
        component = pod.labels.get("app.kubernetes.io/component")
        if component in desired_services:
            by_service[component] = pod

    missing_services: list[str] = []
    for svc, role in desired_services.items():
        pod = by_service.get(svc)
        if pod is None:
            status = "missing"
            container_id = None
            missing_services.append(svc)
        else:
            status = _derive_pod_status(pod)
            container_id = (pod.name[:24]) or None

        prev = _status_history.get(svc)
        if prev is None or prev[0] != status:
            _status_history[svc] = (status, now_mono)
            since_wall = now_wall
        else:
            since_wall = now_wall - (now_mono - prev[1])

        out[svc] = {
            "role": role,
            "status": status,
            "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_wall)),
            "container_id": container_id,
        }

    if auto_heal and missing_services:
        log.warning(
            "supervisor.watchdog.missing_services",
            missing=missing_services,
        )
        # Re-applying the HelmChart CR is cheap (helm-controller
        # short-circuits when nothing changed) but lets the
        # supervisor re-assert desired state when a pod's been
        # ``kubectl delete``'d out from under us.
        result = apply_role_assignment(list(desired_services.values()), env_file)
        log.info(
            "supervisor.watchdog.heal_attempted",
            state=result.state,
            reason=result.reason,
            started=list(result.started),
        )

    return out


def check_health(
    env_file: Path,
    *,
    auto_heal: bool = True,
) -> dict[str, dict[str, Any]]:
    """One watchdog pass.

    Returns a JSON-serialisable ``role_health`` dict keyed by service
    name. Empty dict when no roles are assigned (idle appliance —
    nothing to watch). ``auto_heal=False`` lets tests drive the
    watcher without re-firing the apply path.

    Phase 7: k3s-only. The pre-Phase-7 runtime branch (compose vs
    k3s) is gone with the rest of docker.
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

    out = _check_health_k3s(env_file, desired_services, auto_heal=auto_heal)

    # Garbage-collect stale entries (operator switched roles).
    for stale in list(_status_history):
        if stale not in desired_services:
            del _status_history[stale]

    return out


__all__ = [
    "ServiceHealth",
    "check_health",
    "read_assigned_profiles",
]
