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

import hashlib
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import structlog

from . import appliance_state, approval_state, docker_api
from .cert_auth import build_auth_headers, load_cert, save_cert
from .config import SupervisorConfig
from .firewall_renderer import FirewallProfile, render_drop_in
from .identity import Identity
from .role_orchestrator import (
    compute_target_env,
    probe_port_conflicts,
    render_env_file,
)
from . import watchdog
from .service_lifecycle import apply_role_assignment

# #170 Wave C2 — role-driven compose env file. Written under the
# supervisor's state-dir so it survives slot swaps; the operator's
# baked compose file references it via ``--env-file`` (Wave C3
# subprocess piece). C2 ships only the env render; C3 wires the
# actual ``docker compose up -d`` invocation.
_ROLE_ENV_FILENAME = "role-compose.env"

# #170 Wave C3 — nftables drop-in path. Lives under /etc/nftables.d
# (bind-mounted rw on the supervisor compose entry). The host's
# master /etc/nftables.conf includes everything in that dir under
# the inet filter table's input chain. Strict appliance-only —
# skipped on dev / docker / k8s deployments via the same
# detect_deployment_kind() gate the trigger-file writers use.
_NFT_DROPIN_PATH = Path("/etc/nftables.d/spatium-role.nft")
# Per-profile "last applied" sidecar so we don't re-run ``nft -f``
# every heartbeat when nothing has changed. Lives in the same dir
# as the drop-in so a host-side audit / backup picks it up too.
_NFT_LAST_PROFILE_PATH = Path("/etc/nftables.d/spatium-role.profile")


_CAPABILITY_IMAGES = {
    "can_run_dns_bind9": "ghcr.io/spatiumddi/dns-bind9",
    "can_run_dns_powerdns": "ghcr.io/spatiumddi/dns-powerdns",
    "can_run_dhcp": "ghcr.io/spatiumddi/dhcp-kea",
}


# 5-min cache of the docker daemon's repo list. Image set on an
# appliance changes only on slot upgrade or operator ``docker pull``;
# re-probing per heartbeat is wasted CPU on a 1-CPU VM. Cache keyed
# on monotonic time so wall-clock skew can't fool it.
_REPO_CACHE_TTL_S = 300.0
_repo_cache: tuple[float, set[str]] | None = None


def _cached_image_repos() -> set[str]:
    """Return the set of repo names loaded in the docker daemon,
    cached for ``_REPO_CACHE_TTL_S`` to avoid hammering /var/run/
    docker.sock with one HTTP round-trip per ``can_run_*`` probe per
    heartbeat. ``capabilities`` reporting + role-checkbox enablement
    on the Fleet drilldown are the only callers; both can tolerate a
    5-minute staleness window in exchange for ~70 % less docker
    daemon traffic during steady-state heartbeats."""
    global _repo_cache
    now = time.monotonic()
    if _repo_cache is not None:
        ts, repos = _repo_cache
        if now - ts < _REPO_CACHE_TTL_S:
            return repos
    repos = docker_api.list_image_repos()
    _repo_cache = (now, repos)
    return repos


def _docker_image_present(repo: str) -> bool:
    """Return True if at least one tag of ``repo`` is loaded into the
    host docker daemon, using the cached repo list.

    Uses the docker engine API directly via /var/run/docker.sock
    instead of shelling out to the ``docker`` CLI — same data, ~30×
    faster per call (no Go binary startup + arg parsing). The
    capability path queries this for three repos per heartbeat;
    batching through the cache makes it effectively one call per
    5 minutes during steady state.

    A substring-aware match handles both fully-qualified
    (``ghcr.io/spatiumddi/dns-bind9``) and short-form
    (``spatiumddi/dns-bind9``, ``dns-bind9``) tagging the bake might
    produce on different rebuild paths.
    """
    repos = _cached_image_repos()
    short = repo.rsplit("/", 1)[-1]
    return any(r == repo or r.endswith("/" + short) or r == short for r in repos)


def _detect_storage_type() -> str:
    """Return ``"ssd"`` / ``"hdd"`` / ``"unknown"`` based on the root
    block device's ``rotational`` flag. The supervisor reads /sys/
    via the host bind mount; rotational=0 → ssd, =1 → hdd."""
    try:
        # /proc/mounts identifies what's mounted on / — first whitespace
        # field is the source device. Strip ``/dev/`` + any trailing
        # digits (sda3 → sda, nvme0n1p3 → nvme0n1).
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    dev = parts[0]
                    if dev.startswith("/dev/"):
                        name = dev[5:]
                        # Strip trailing partition digits, but preserve
                        # nvme's pN suffix by stripping until a non-digit.
                        while name and name[-1].isdigit():
                            name = name[:-1]
                        # nvme0n1p3 → nvme0n1p → nvme0n1 (one more strip)
                        if name.endswith("p"):
                            name = name[:-1]
                        rot_path = f"/sys/block/{name}/queue/rotational"
                        try:
                            with open(rot_path, "r", encoding="utf-8") as r:
                                return "hdd" if r.read().strip() == "1" else "ssd"
                        except OSError:
                            return "unknown"
                    break
    except OSError:
        pass
    return "unknown"


def _detect_host_nics() -> list[str]:
    """Return the appliance's physical / virtual NIC names (e.g.
    ``["ens18", "eth0"]``). Skips loopback, docker bridges, virtual
    veth pairs, and the container's own bridge interface so the list
    is the operator-visible host hardware."""
    nics: list[str] = []
    try:
        import os

        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            if name.startswith(("docker", "br-", "veth", "virbr", "tailscale")):
                continue
            nics.append(name)
    except OSError:
        pass
    return nics


def _capabilities_payload() -> dict[str, Any]:
    """Build the supervisor-capabilities block reported on every
    heartbeat. Matches the schema in issue #170 ("Multi-role +
    capability reporting"):

    * ``can_run_dns_bind9`` / ``can_run_dns_powerdns`` /
      ``can_run_dhcp`` — true when the corresponding service image
      is loaded in the host docker daemon (appliance bake pre-loads
      every service image so all three come back true on
      Application appliances). The Fleet drilldown disables role
      checkboxes when these are false.
    * ``can_run_observer`` — always true. The observer role is a
      pure supervisor-side metrics/log shipper with no separate
      service container, so any approved supervisor can run it.
    * ``has_baked_images`` — true on appliance deployments (where
      ``spatium-docker-overlay.service`` loop-mounts the baked
      docker-overlay.img).
    * ``supervisor_version`` — packaging metadata.
    * ``cpu_count`` / ``memory_mb`` — host capacity from /proc.
    * ``storage_type`` — ``"ssd"`` / ``"hdd"`` / ``"unknown"``.
    * ``host_nics`` — physical / virtual NIC names (lo + docker
      bridges filtered out).
    """
    out: dict[str, Any] = {
        "has_baked_images": appliance_state.detect_deployment_kind() == "appliance",
        "supervisor_version": _supervisor_version(),
        # observer is always compatible (#170 multi-role table:
        # "always compatible") — no service container needed, the
        # supervisor itself is the observer.
        "can_run_observer": True,
    }
    for cap_field, repo in _CAPABILITY_IMAGES.items():
        out[cap_field] = _docker_image_present(repo)
    try:
        import os

        cpu = os.cpu_count()
        if cpu is not None:
            out["cpu_count"] = cpu
    except Exception:  # noqa: BLE001
        pass
    try:
        # /proc/meminfo is small + parseable without psutil.
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    out["memory_mb"] = kb // 1024
                    break
    except OSError:
        pass
    out["storage_type"] = _detect_storage_type()
    out["host_nics"] = _detect_host_nics()
    return out


def _supervisor_version() -> str:
    from . import __version__

    return __version__


# #170 Wave E — periodic service-container watchdog. Heartbeat
# carries the watchdog's last verdict to the control plane (one
# entry per assigned compose service). Watchdog itself fires every
# ``_WATCHDOG_INTERVAL_S`` (5 min by default) — same shape as the
# firewall drift check below: a short throttled run shared across
# multiple heartbeats so the watchdog signal is fresh without
# spamming the docker daemon every minute.
_WATCHDOG_INTERVAL_S = 300.0  # 5 minutes
_last_watchdog_at: float = 0.0
_cached_role_health: dict[str, Any] = {}


def _watchdog_check_due() -> bool:
    """First call always returns True (forces a probe within the
    first watchdog cadence after startup); subsequent calls gate on
    monotonic elapsed."""
    return time.monotonic() - _last_watchdog_at >= _WATCHDOG_INTERVAL_S


# Periodic firewall live-ruleset verification. Read the actual
# kernel-active rules from nft and confirm the supervisor-managed
# service ports per the assigned role profile are present. If any
# expected rule is missing (someone flushed the ruleset, the host
# nftables.conf got out from under us, the drop-in include failed
# silently, etc.) clear the in-memory "body matched" short-circuit
# so the next ``_maybe_apply_firewall`` rewrite + nft reload
# restores the canonical state.
_FIREWALL_LIVE_CHECK_INTERVAL_S = 300.0  # 5 minutes
_last_firewall_live_check_at: float = 0.0


def _firewall_live_check_due() -> bool:
    """Return True if the periodic live-ruleset check is due. The
    cadence is in monotonic time so the supervisor's own restart
    doesn't accidentally skip an interval. First call after startup
    always returns True (forces an early verification once the
    supervisor settles)."""
    return (
        time.monotonic() - _last_firewall_live_check_at
        >= _FIREWALL_LIVE_CHECK_INTERVAL_S
    )


def _firewall_live_missing_ports(profile: FirewallProfile) -> tuple[set[int], set[int]]:
    """Return the (missing_tcp, missing_udp) port sets — expected
    rules per the rendered ``profile`` that aren't visible in the
    kernel's live ruleset. Empty pair = firewall is in the expected
    shape. Both pairs populated = either nft is unreachable (treat
    as missing — force re-apply) or the drop-in include path is
    broken.

    Reads via ``nft -j list chain inet filter input`` (JSON, the
    machine-readable output mode added in nftables 0.9.x); JSON is
    cheaper to parse than the human format + immune to translation
    drift across distro versions.
    """
    if not profile.expected_tcp_ports and not profile.expected_udp_ports:
        return set(), set()
    try:
        result = subprocess.run(
            ["nft", "-j", "list", "chain", "inet", "filter", "input"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # nft missing entirely → treat as "all rules missing" so the
        # caller force re-applies (which itself will fail with a
        # clearer error if the host genuinely lacks nft).
        return set(profile.expected_tcp_ports), set(profile.expected_udp_ports)
    if result.returncode != 0:
        return set(profile.expected_tcp_ports), set(profile.expected_udp_ports)
    try:
        import json

        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return set(profile.expected_tcp_ports), set(profile.expected_udp_ports)
    # The nft JSON shape is {"nftables": [<rule objects>]}. Each
    # rule object has an "expr" array of expression atoms. For our
    # role-driven accept rules the relevant atoms are a payload
    # match (tcp/udp dport == <port>) followed by a verdict (accept).
    tcp_present: set[int] = set()
    udp_present: set[int] = set()
    for entry in data.get("nftables") or []:
        rule = entry.get("rule")
        if not rule:
            continue
        # Walk the expression atoms looking for payload match against
        # tcp.dport or udp.dport that matches a literal integer.
        for atom in rule.get("expr") or []:
            match = atom.get("match")
            if not match:
                continue
            left = match.get("left") or {}
            right = match.get("right")
            payload = left.get("payload") or {}
            proto = payload.get("protocol")
            field = payload.get("field")
            if field != "dport":
                continue
            # ``right`` is either an int (single port) or an object
            # like ``{"set": [80, 443]}`` for a port set. Normalise.
            ports: list[int] = []
            if isinstance(right, int):
                ports.append(right)
            elif isinstance(right, dict) and "set" in right:
                for item in right["set"]:
                    if isinstance(item, int):
                        ports.append(item)
            if proto == "tcp":
                tcp_present.update(ports)
            elif proto == "udp":
                udp_present.update(ports)
    missing_tcp = set(profile.expected_tcp_ports) - tcp_present
    missing_udp = set(profile.expected_udp_ports) - udp_present
    return missing_tcp, missing_udp


def _maybe_apply_firewall(
    role_assignment: dict[str, Any] | None,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Render + atomically swap the supervisor's nftables drop-in.

    Strict appliance-only gate (mirrors the trigger-file writers in
    ``appliance_state.py``): on docker / k8s / unknown deployments
    the /etc/nftables.d bind mount may not exist + nft itself may
    not be installed, so we no-op silently.

    Three short-circuit signals:

    1. ``detect_deployment_kind() != "appliance"`` → log + bail.
    2. Last-applied profile sidecar matches the freshly-rendered
       profile body → no-op (most heartbeats land here).
    3. ``nft -c -f <tmp>`` dry-run fails → log loud + leave the
       live drop-in untouched. The operator's invalid extra
       fragment can't put the firewall into a half-rendered state.

    Atomic-rename on success so a crash mid-write doesn't truncate
    the live file. ``nft -f`` after the rename reloads the master
    /etc/nftables.conf and picks up the new drop-in.
    """
    if appliance_state.detect_deployment_kind() != "appliance":
        return

    global _last_firewall_live_check_at

    profile: FirewallProfile = render_drop_in(role_assignment)
    body = profile.body

    # Short-circuit on unchanged body. We compare the live drop-in
    # file directly rather than caching in-memory because a host-
    # side intervention (operator manually edited the file) should
    # cause us to re-apply on the next heartbeat, restoring the
    # canonical state.
    try:
        current_body = _NFT_DROPIN_PATH.read_text(encoding="utf-8")
    except OSError:
        current_body = ""
    body_matches = current_body == body

    # Periodic live-ruleset drift check (#170 Wave D follow-up). The
    # drop-in file content matching the desired body only proves the
    # FILE is right — not that the kernel-active nftables ruleset
    # actually includes those rules. ``nft -f /etc/nftables.conf``
    # could have failed silently after an out-of-band edit, the host
    # could have ``nft flush ruleset``'d during a manual debugging
    # session, or the master conf's ``include`` directive could
    # have stopped matching the drop-in path. Every
    # ``_FIREWALL_LIVE_CHECK_INTERVAL_S`` (5 min) we read the live
    # ruleset and confirm the expected per-role service ports are
    # present; if any are missing, force a re-apply by bypassing the
    # body-matches short-circuit.
    force_apply = False
    if body_matches and _firewall_live_check_due():
        missing_tcp, missing_udp = _firewall_live_missing_ports(profile)
        _last_firewall_live_check_at = time.monotonic()
        if missing_tcp or missing_udp:
            log.warning(
                "supervisor.firewall.drift_detected",
                profile=profile.name,
                missing_tcp=sorted(missing_tcp),
                missing_udp=sorted(missing_udp),
            )
            force_apply = True

    if body_matches and not force_apply:
        return

    try:
        _NFT_DROPIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _NFT_DROPIN_PATH.with_suffix(".new")
        tmp.write_text(body, encoding="utf-8")
        # Dry-run validate before swap. The drop-in is included from
        # inside ``table inet filter { chain input { ... } }`` in the
        # host's ``/etc/nftables.conf``, so the body itself is a chain
        # fragment — not a complete nft script. Running ``nft -c -f``
        # against the fragment alone fails with "syntax error,
        # unexpected tcp" because nft expects a top-level table
        # declaration. Wrap the fragment in the same chain context
        # the live config uses, write the wrapped form to a *second*
        # temp file, validate that, then swap the *unwrapped* form
        # into the live drop-in path.
        wrapped_tmp = _NFT_DROPIN_PATH.with_suffix(".check.new")
        wrapped_body = (
            "table inet filter {\n"
            "    chain input {\n"
            + "\n".join("        " + line for line in body.splitlines())
            + "\n    }\n"
            + "}\n"
        )
        wrapped_tmp.write_text(wrapped_body, encoding="utf-8")
        result = subprocess.run(
            ["nft", "-c", "-f", str(wrapped_tmp)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        try:
            wrapped_tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if result.returncode != 0:
            log.warning(
                "supervisor.firewall.dry_run_failed",
                profile=profile.name,
                stderr=result.stderr.strip()[:300],
            )
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        tmp.replace(_NFT_DROPIN_PATH)
        # Reload nftables so the master conf picks up the new
        # drop-in. ``nft -f /etc/nftables.conf`` is the documented
        # reload path; it's idempotent + atomic on the kernel side
        # (the netlink commit either lands fully or not at all).
        reload_result = subprocess.run(
            ["nft", "-f", "/etc/nftables.conf"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if reload_result.returncode != 0:
            log.warning(
                "supervisor.firewall.reload_failed",
                profile=profile.name,
                stderr=reload_result.stderr.strip()[:300],
            )
            return
        _NFT_LAST_PROFILE_PATH.write_text(profile.name + "\n", encoding="utf-8")
        log.info(
            "supervisor.firewall.applied",
            profile=profile.name,
            roles=role_assignment.get("roles") if role_assignment else [],
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("supervisor.firewall.apply_failed", error=str(exc))


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
        # #170 Phase E2 — probe UDP+TCP/53 + UDP/67 every tick. Empty
        # dict explicitly clears any prior conflict server-side;
        # ``None`` would skip the overwrite, which isn't what we want
        # if the conflict went away. The probe is cheap (``ss``) so
        # running it every heartbeat is fine.
        "port_conflicts": probe_port_conflicts(),
    }
    # #170 Wave D follow-up — surface the outcome of the previous
    # heartbeat's compose-lifecycle apply. Empty / None on the first
    # heartbeat or before any role assignment has been issued.
    last_lifecycle_state, last_lifecycle_reason = _read_lifecycle_state(cfg.state_dir)
    if last_lifecycle_state is not None:
        body["role_switch_state"] = last_lifecycle_state
        if last_lifecycle_reason is not None:
            body["role_switch_reason"] = last_lifecycle_reason

    # #170 Wave E — service-container watchdog. Every
    # ``_WATCHDOG_INTERVAL_S`` we snapshot the running containers and
    # diff against the supervisor's persisted role assignment; the
    # result rides on every heartbeat (cached between watchdog runs so
    # the Fleet UI doesn't see the field flicker between probes). On
    # the appliance only — docker/k8s deployments don't run this
    # lifecycle path, so the watchdog has nothing to watch.
    global _last_watchdog_at, _cached_role_health
    if appliance_state.detect_deployment_kind() == "appliance":
        if _watchdog_check_due():
            try:
                env_file = cfg.state_dir / _ROLE_ENV_FILENAME
                _cached_role_health = watchdog.check_health(env_file)
                _last_watchdog_at = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                # Never let a watchdog crash kill the heartbeat path.
                log.warning("supervisor.watchdog.crashed", error=str(exc))
        if _cached_role_health:
            body["role_health"] = _cached_role_health
    url_path = "/api/v1/appliance/supervisor/heartbeat"
    url = cfg.control_plane_url.rstrip("/") + url_path

    # #170 Wave D follow-up — cert auth supersedes session-token
    # auth once the cert is on disk. Build the headers + sign with
    # the supervisor's Ed25519 private key; the backend's
    # cert_auth.py middleware validates the chain + signature + the
    # timestamp skew. When no cert yet (pre-approval), fall through
    # to the session_token body field — same shape as today.
    cached_cert = load_cert(cfg.state_dir)
    headers: dict[str, str] = {}
    if cached_cert is not None:
        try:
            headers = build_auth_headers(
                "POST", url_path, cached_cert, identity.private_key, appliance_id
            )
            # Once we have a cert the session token shouldn't ride
            # along — keeps the wire payload clean + makes server-
            # side cert-only enforcement straightforward when it
            # lands.
            body.pop("session_token", None)
        except Exception as exc:  # noqa: BLE001
            log.warning("supervisor.heartbeat.cert_auth_skipped", error=str(exc))

    try:
        resp = client.post(url, json=body, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        # Transient network / DNS / timeout — don't count toward
        # revocation strikes. The control plane is unreachable, not
        # rejecting us; once it comes back the 200 path resumes.
        log.warning("supervisor.heartbeat.failed", error=str(exc))
        return
    if resp.status_code == 403 or resp.status_code == 404:
        # 403 = approval revoked or cert no longer valid for any
        # known appliance row. 404 = appliance row deleted, or the
        # control plane's supervisor_registration_enabled flag is
        # off. Both mean "you shouldn't be talking to me anymore" —
        # increment the consecutive-strike counter; flip to
        # ``revoked`` once REVOCATION_STRIKE_LIMIT in a row, which
        # de-noises a short control-plane restart.
        prior_state = approval_state.read_state(cfg.state_dir)
        new_state, strikes = approval_state.record_revocation_signal(cfg.state_dir)
        log.warning(
            "supervisor.heartbeat.rejected",
            status_code=resp.status_code,
            strikes=strikes,
            new_state=new_state,
            appliance_id=str(appliance_id),
        )
        # Tear down any supervised service containers when crossing
        # the threshold from approved → revoked. The control plane
        # has explicitly disowned us; leaving the DNS/DHCP daemons
        # running would have them serve stale config against clients
        # that no longer have a config sync path. Explicit operator
        # intent (deleted the row) is distinct from Non-negotiable #5
        # (cache + keep running when control plane is unreachable);
        # rejection isn't unreachable.
        if new_state == "revoked" and prior_state != "revoked":
            if appliance_state.detect_deployment_kind() == "appliance":
                try:
                    env_path = cfg.state_dir / _ROLE_ENV_FILENAME
                    lifecycle = apply_role_assignment([], env_path)
                    log.warning(
                        "supervisor.heartbeat.revoked_teardown",
                        state=lifecycle.state,
                        stopped=list(lifecycle.stopped),
                        reason=lifecycle.reason,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "supervisor.heartbeat.revoked_teardown_failed",
                        error=str(exc),
                    )
        return
    if resp.status_code >= 500:
        # 5xx is the control plane crashing mid-flight, not a
        # deliberate rejection — don't count toward revocation.
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
    # Heartbeat accepted — clear any prior strike counter and stamp
    # ``approved`` if we weren't there yet.
    approval_state.record_success(cfg.state_dir)

    try:
        body_out = resp.json()
    except ValueError:
        log.warning("supervisor.heartbeat.bad_json")
        return

    # #170 Wave D follow-up — pick up cert + CA chain on the first
    # heartbeat after approval. Subsequent heartbeats include the
    # same bytes; ``save_cert`` is content-addressed so re-saving
    # the same body is a disk no-op.
    cert_pem = body_out.get("cert_pem")
    ca_chain_pem = body_out.get("ca_chain_pem")
    if cert_pem and ca_chain_pem:
        try:
            save_cert(cfg.state_dir, cert_pem, ca_chain_pem)
            if cached_cert is None:
                log.info(
                    "supervisor.heartbeat.cert_received",
                    cert_expires_at=body_out.get("cert_expires_at"),
                )
        except OSError as exc:
            log.warning("supervisor.heartbeat.cert_save_failed", error=str(exc))

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

    # #170 Wave C3 — render + apply the nftables drop-in *before*
    # the compose env so the firewall lands before the matching
    # service container would start (when C3's compose subprocess
    # lands). Even today, no-op on docker / k8s deployments via the
    # appliance gate inside _maybe_apply_firewall.
    _maybe_apply_firewall(role_assignment, log)

    target = compute_target_env(role_assignment)
    # #170 Wave D follow-up — the role env file carries ONLY role-
    # scoped vars (``COMPOSE_PROFILES``, ``AGENT_GROUP``,
    # ``DNS_ENGINE``). The compose-service interpolation for static
    # appliance config (``${SPATIUMDDI_VERSION}``,
    # ``${CONTROL_PLANE_URL}``, ``${APPLIANCE_HOSTNAME}``,
    # ``${DNS_AGENT_KEY}``, ``${DOCKER_GID}``, ...) is satisfied by
    # the host's main ``/etc/spatiumddi/.env`` — see
    # ``service_lifecycle._HOST_ENV_FILE``, which is passed to
    # ``docker compose`` as an additional ``--env-file`` ahead of
    # this one. That keeps the role env file scoped to what the
    # supervisor actually decides and avoids stale duplication when
    # the host .env is upgraded out-of-band.
    env_path = cfg.state_dir / _ROLE_ENV_FILENAME
    env_write_failed = False
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = render_env_file(target)
        # Atomic write — partial files would confuse the compose
        # subprocess below if the supervisor crashed mid-write.
        tmp = env_path.with_suffix(".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(env_path)
        log.info(
            "supervisor.heartbeat.role_env_rendered",
            profiles=target.profiles,
            env_path=str(env_path),
        )
    except OSError as exc:
        env_write_failed = True
        log.warning(
            "supervisor.heartbeat.role_env_write_failed",
            error=str(exc),
            env_path=str(env_path),
        )

    # #170 Wave D follow-up — actually run ``docker compose`` against
    # the freshly-written env file so the assigned service container
    # comes up (or comes down on de-assignment). Best-effort — on
    # failure we log + carry the failure state up to the control
    # plane in the next heartbeat's ``role_switch_state``.
    #
    # Skip the apply when the rendered env file content hash is
    # unchanged from the last successful apply. The previous "fire
    # every heartbeat" shape ran ``docker compose ps`` + ``up -d``
    # every 60 s even during steady state when nothing had changed.
    # Each subprocess pair costs ~600 ms on a 1-CPU VM (Go binary
    # startup + arg parsing + JSON formatting); 60-second cadence × 24h
    # = ~14 minutes of wasted CPU per day on a fleet that wasn't
    # transitioning anything. The sidecar hash file is reset on
    # supervisor restart so a fresh boot always re-applies once.
    # #170 Wave E follow-up — if the appliance row was deleted on the
    # control plane and we tripped the revocation threshold above
    # (well, on a prior heartbeat — the 200 path above wouldn't have
    # been reached if we were rejected now), stop touching the local
    # compose state. The cached role-compose.env is stale by
    # definition and re-applying it just keeps the supervisor sliding
    # toward a state the control plane no longer expects. The console
    # dashboard surfaces the red ``Approval revoked`` chip so the
    # operator knows the recovery is "re-pair from /appliance/pairing".
    if approval_state.read_state(cfg.state_dir) == "revoked":
        log.info("supervisor.heartbeat.lifecycle_skipped_revoked")
        _ = identity
        return

    if not env_write_failed and appliance_state.detect_deployment_kind() == "appliance":
        env_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        last_hash = _read_last_apply_hash(cfg.state_dir)
        if env_hash == last_hash:
            log.info(
                "supervisor.heartbeat.lifecycle_skipped",
                reason="env_unchanged",
                env_hash=env_hash[:12],
            )
        else:
            lifecycle = apply_role_assignment(target.profiles, env_path)
            log.info(
                "supervisor.heartbeat.lifecycle_applied",
                state=lifecycle.state,
                reason=lifecycle.reason,
                started=list(lifecycle.started),
                stopped=list(lifecycle.stopped),
            )
            # The state + reason are returned on the NEXT heartbeat (not
            # this one — we've already POSTed). Cache them on disk so a
            # supervisor restart doesn't lose them.
            _persist_lifecycle_state(cfg.state_dir, lifecycle.state, lifecycle.reason)
            # The runtime set just changed (or attempted to) — invalidate
            # the watchdog cache so the next heartbeat re-probes
            # immediately rather than rendering 5-min-stale health.
            # ``_last_watchdog_at`` is already declared global at the
            # top of this function (one declaration per scope rule).
            _last_watchdog_at = 0.0
            # Only stamp the hash on success — a failed apply should
            # re-attempt on the next heartbeat (the failure may have
            # been transient: image pull glitch, transient port
            # conflict, etc).
            if lifecycle.state in ("ready", "idle"):
                _write_last_apply_hash(cfg.state_dir, env_hash)

    # Identity unused in C2's payload but kept on the signature so
    # C2's mTLS upgrade doesn't need to thread it back in. Silence
    # the linter without adding a runtime cost.
    _ = identity


_LIFECYCLE_STATE_FILE = "role-switch-state"
_LAST_APPLY_HASH_FILE = "role-compose.env.hash"


def _read_last_apply_hash(state_dir: Path) -> str | None:
    """Return the env-file content hash of the last successful
    ``apply_role_assignment``, or ``None`` on first boot / no prior
    apply / file missing. Used by the heartbeat to skip the
    subprocess pair when nothing has changed."""
    path = state_dir / _LAST_APPLY_HASH_FILE
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_last_apply_hash(state_dir: Path, env_hash: str) -> None:
    """Stamp the env-file content hash so subsequent heartbeats can
    skip the apply when the rendered env is unchanged. Atomic write
    so a supervisor crash mid-flush can't leave a torn file that
    would silently skip a real divergence."""
    path = state_dir / _LAST_APPLY_HASH_FILE
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(env_hash + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _persist_lifecycle_state(state_dir: Path, state: str, reason: str | None) -> None:
    """Write the most recent compose-lifecycle outcome to disk so the
    NEXT heartbeat reports it (this one has already left)."""
    path = state_dir / _LIFECYCLE_STATE_FILE
    payload = state
    if reason:
        payload += "\n" + reason
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _read_lifecycle_state(state_dir: Path) -> tuple[str | None, str | None]:
    """Read the last persisted lifecycle outcome. Returns
    ``(state, reason)`` — both ``None`` when no prior pass."""
    path = state_dir / _LIFECYCLE_STATE_FILE
    if not path.exists():
        return None, None
    try:
        body = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    state = body[0].strip() if body else None
    reason = "\n".join(body[1:]).strip() or None if len(body) > 1 else None
    return state, reason
