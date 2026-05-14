"""Periodic heartbeat to the control plane (#170 Wave C1).

Replaces the appliance-host telemetry the DNS / DHCP service agents
used to ship on their own per-row heartbeats in #138 Phase 8f-2. The
supervisor is now the single producer; service-container heartbeats
keep carrying service-level state (last_seen, agent_version, server
identity) but the appliance-host block (slot, deployment_kind,
upgrade state, snmpd/chrony status) lives here.

Loop shape:

1. Snapshot ``appliance_state.collect()`` for telemetry.
2. POST to ``/api/v1/appliance/supervisor/heartbeat`` with the
   appliance_id + telemetry + capabilities.
3. Backend persists + returns ``{desired_appliance_version,
   desired_slot_image_url, reboot_requested}``.
4. Compare desired to the local state; fire trigger files via
   ``appliance_state.maybe_fire_*``. Idempotent — trigger-file
   presence guards against double-firing.
5. Sleep ``heartbeat_interval_seconds``; repeat.

Auth: session-token interim. The supervisor cached the cleartext
token from the register response; we present it on every heartbeat
until the cert-issuance path lands the mTLS switch in C2/D. Approved
appliances no longer carry a session_token server-side; the backend
accepts heartbeat from any approved row in this interim window
(see SupervisorHeartbeatRequest docstring).

Network failures are logged but never raise into the caller — the
loop keeps running so a transient control-plane outage doesn't kill
the supervisor.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog

from . import appliance_state
from .config import SupervisorConfig
from .identity import Identity
from .role_orchestrator import compute_target_env, render_env_file


# #170 Wave C2 — role-driven compose env file. Written under the
# supervisor's state-dir so it survives slot swaps; the operator's
# baked compose file references it via ``--env-file`` (Wave C3
# subprocess piece). C2 ships only the env render; C3 wires the
# actual ``docker compose up -d`` invocation.
_ROLE_ENV_FILENAME = "role-compose.env"


def _capabilities_payload() -> dict[str, Any]:
    """Build the supervisor-capabilities block reported on every
    heartbeat. The fields are read locally — psutil for hardware,
    docker image inspect would tell us about can_run_* but the C1
    cut doesn't ship that yet (it lands with role assignment in C2).
    For now we ship just the host-level facts the supervisor can
    cheaply derive."""
    out: dict[str, Any] = {
        "has_baked_images": appliance_state.detect_deployment_kind() == "appliance",
        "supervisor_version": _supervisor_version(),
    }
    try:
        import os

        cpu = os.cpu_count()
        if cpu is not None:
            out["cpu_count"] = cpu
    except Exception:  # noqa: BLE001
        pass
    try:
        import os

        # /proc/meminfo is small + parseable without psutil.
        meminfo = open("/proc/meminfo", "r", encoding="utf-8").read()
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                out["memory_mb"] = kb // 1024
                break
        del os
    except Exception:  # noqa: BLE001
        pass
    return out


def _supervisor_version() -> str:
    from . import __version__

    return __version__


def heartbeat_once(
    cfg: SupervisorConfig,
    appliance_id: uuid.UUID,
    session_token: str | None,
    identity: Identity,
    client: httpx.Client,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """One heartbeat round-trip + trigger-file follow-up.

    Never raises. Logs every error path so a real outage shows up in
    journalctl without taking down the supervisor process.
    """
    state = appliance_state.collect()
    body: dict[str, Any] = {
        "appliance_id": str(appliance_id),
        "session_token": session_token,
        "capabilities": _capabilities_payload(),
        **state,
    }
    url = cfg.control_plane_url.rstrip("/") + "/api/v1/appliance/supervisor/heartbeat"
    try:
        resp = client.post(url, json=body, timeout=10.0)
    except httpx.HTTPError as exc:
        log.warning("supervisor.heartbeat.failed", error=str(exc))
        return
    if resp.status_code == 403:
        # Approval revoked / row deleted. The supervisor's next
        # registration attempt would land it back in pending — but we
        # don't tear down the local identity here. C2/D's deeper
        # state machine handles the "fall back to pairing" path.
        log.warning("supervisor.heartbeat.forbidden", appliance_id=str(appliance_id))
        return
    if resp.status_code == 404:
        # Module disabled (supervisor_registration_enabled flipped
        # off mid-flight) or row deleted. Same shape as 403 — log +
        # keep idling so a re-enable picks the supervisor back up
        # without a restart.
        log.warning("supervisor.heartbeat.not_found")
        return
    if resp.status_code >= 500:
        log.warning(
            "supervisor.heartbeat.server_error",
            status_code=resp.status_code,
        )
        return
    if resp.status_code != 200:
        log.warning(
            "supervisor.heartbeat.unexpected_status",
            status_code=resp.status_code,
        )
        return

    try:
        body_out = resp.json()
    except ValueError:
        log.warning("supervisor.heartbeat.bad_json")
        return

    desired_version = body_out.get("desired_appliance_version")
    desired_url = body_out.get("desired_slot_image_url")
    reboot_requested = bool(body_out.get("reboot_requested"))

    if desired_version and desired_url:
        if appliance_state.maybe_fire_fleet_upgrade(desired_version, desired_url):
            log.info(
                "supervisor.heartbeat.upgrade_trigger_fired",
                desired_version=desired_version,
            )
    if reboot_requested:
        if appliance_state.maybe_fire_reboot(True):
            log.info("supervisor.heartbeat.reboot_trigger_fired")

    # #170 Wave C2 — render the role-driven compose env. C3 will
    # consume this via ``docker compose --env-file`` to actually
    # bring services up/down; for now we just write the file so the
    # operator can inspect what the supervisor would do next.
    role_assignment = body_out.get("role_assignment") or {}
    target = compute_target_env(role_assignment)
    env_path = cfg.state_dir / _ROLE_ENV_FILENAME
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = render_env_file(target)
        # Atomic write — partial files would confuse C3's compose
        # subprocess if the supervisor crashed mid-write.
        tmp = env_path.with_suffix(".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(env_path)
        log.info(
            "supervisor.heartbeat.role_env_rendered",
            profiles=target.profiles,
            env_path=str(env_path),
        )
    except OSError as exc:
        log.warning(
            "supervisor.heartbeat.role_env_write_failed",
            error=str(exc),
            env_path=str(env_path),
        )

    # Identity unused in C2's payload but kept on the signature so
    # C2's mTLS upgrade doesn't need to thread it back in. Silence
    # the linter without adding a runtime cost.
    _ = identity
