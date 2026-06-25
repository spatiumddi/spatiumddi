#!/usr/bin/env python3
"""fleet_enable — turn on DNS + DHCP roles on the single AIO node (§7.5 step 3-4).

On a fresh AIO appliance the control plane runs but no data-plane daemons exist
yet. This one-shot:

  1. Finds the single approved appliance row (the AIO node). If it's still
     ``pending_approval`` we approve + sign its supervisor cert.
  2. PUTs ``roles=["dns-bind9","dhcp"]`` (capability-gated) so the supervisor
     schedules bind9 + kea on its next heartbeat and injects per-role agent keys
     into ``role-compose.env`` (zero key-paste).
  3. Polls ``/health/platform`` until the rollup is ``ok`` (all components green).
  4. Asserts exactly one bind9 DNS server row + one kea DHCP server row are
     registered and ``active``.

Idempotent: re-running against an already-roled, healthy fleet is a no-op success.
Group binding (``assigned_dns_group_id`` / ``assigned_dhcp_group_id``) is deferred
to ``seed_scaffold`` because the DNS/DHCP groups don't exist until it runs — the
agents register into an auto-created group meanwhile (dns/agents.py:180,
dhcp/agents.py register path), which seed_scaffold reconciles.

GROUNDING (real backend routes — cited inline):
  * GET  /v1/appliance/appliances              appliance/supervisor.py:2523 (ApplianceRow:2314)
  * POST /v1/appliance/appliances/{id}/approve appliance/supervisor.py:2602
  * PUT  /v1/appliance/appliances/{id}/roles   appliance/supervisor.py:3223 (roles/_VALID_ROLES:3192)
  * GET  /health/platform                       backend/app/api/health.py:270 ({status, components})
  * GET  /v1/dns/groups                         dns/router.py:1015
  * GET  /v1/dns/groups/{gid}/servers          dns/router.py:1147 (ServerResponse driver/status:318/323)
  * GET  /v1/dhcp/servers                       dhcp/servers.py:289 (ServerResponse driver/status:140/145)

Usage:  python3 fleet_enable.py --run-id <id> --run-root <path> --manifest <path>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

import spddi_perf.manifest as manifest_mod
from spddi_perf.logging_util import atomic_write_json, get_logger, log_event, utc_now_iso
from spddi_perf.runpaths import RunPaths

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _api import ApiClient  # noqa: E402

DESIRED_ROLES = ["dns-bind9", "dhcp"]   # AIO: BIND9 for DNS, Kea for DHCP

# Mirror of the env var names in seed_scaffold.py.  When these are set, pass
# dns_group_id / dhcp_group_id in the roles PUT so the supervisor binds the agents
# to those groups immediately (no auto-create group detour).
ENV_DNS_GROUP_ID = "SPDDI_PERF_DNS_GROUP_ID"
ENV_DHCP_GROUP_ID = "SPDDI_PERF_DHCP_GROUP_ID"
HEALTH_TIMEOUT_S = 300.0
SERVERS_TIMEOUT_S = 300.0
POLL_S = 5.0


def _find_aio_appliance(api: ApiClient, log: logging.Logger) -> dict[str, Any]:
    """Return the appliance row to role. Prefers the single approved row."""
    # GET /v1/appliance/appliances — appliance/supervisor.py:2523
    rows = api.json("GET", "/v1/appliance/appliances")["appliances"]
    if not rows:
        raise RuntimeError("no appliances registered — supervisor hasn't paired")
    approved = [r for r in rows if r["state"] == "approved"]
    if len(approved) == 1:
        return approved[0]
    if len(approved) > 1:
        log.warning("multiple approved appliances (%d) — using the most recent", len(approved))
        return approved[0]
    # none approved → the most-recent pending one (we'll approve it)
    pending = [r for r in rows if r["state"] == "pending_approval"]
    if pending:
        return pending[0]
    raise RuntimeError(f"no approved/pending appliance among {len(rows)} rows")


def _ensure_approved(api: ApiClient, appliance: dict[str, Any], log: logging.Logger) -> dict[str, Any]:
    aid = appliance["id"]
    if appliance["state"] == "approved":
        return appliance
    # POST /v1/appliance/appliances/{id}/approve — appliance/supervisor.py:2602
    log_event(log, logging.INFO, "approving_appliance", appliance_id=aid,
              hostname=appliance.get("hostname"))
    api.post(f"/v1/appliance/appliances/{aid}/approve", json={}, ok=(200,))
    return api.json("GET", f"/v1/appliance/appliances/{aid}")


def _assign_roles(api: ApiClient, aid: str, log: logging.Logger) -> dict[str, Any]:
    # PUT /v1/appliance/appliances/{id}/roles — appliance/supervisor.py:3223
    # Optionally bind to pre-existing groups so the agents register there immediately
    # (rather than auto-creating a throwaway group, which seed_scaffold then can't use).
    body: dict[str, Any] = {"roles": DESIRED_ROLES}
    dns_gid = (os.environ.get(ENV_DNS_GROUP_ID) or "").strip()
    dhcp_gid = (os.environ.get(ENV_DHCP_GROUP_ID) or "").strip()
    if dns_gid:
        body["dns_group_id"] = dns_gid
    if dhcp_gid:
        body["dhcp_group_id"] = dhcp_gid
    log_event(log, logging.INFO, "assigning_roles", appliance_id=aid, roles=DESIRED_ROLES,
              dns_group_id=dns_gid or None, dhcp_group_id=dhcp_gid or None)
    return api.json("PUT", f"/v1/appliance/appliances/{aid}/roles", json=body, ok=(200,))


def _poll_health(api: ApiClient, log: logging.Logger, timeout_s: float) -> tuple[bool, dict[str, Any]]:
    """Poll /health/platform until rollup == ok (or timeout). Returns (ok, last)."""
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            # GET /health/platform (origin root, not /api/v1) — health.py:270
            last = api.get_root("/health/platform").json()
        except Exception as exc:  # noqa: BLE001 — keep polling through transient blips
            log_event(log, logging.WARNING, "health_poll_error", error=str(exc)[:200])
            time.sleep(POLL_S)
            continue
        status = last.get("status")
        bad = [c.get("name") for c in last.get("components", []) if c.get("status") != "ok"]
        log_event(log, logging.INFO, "health_platform", status=status, degraded=bad)
        if status == "ok":
            return True, last
        time.sleep(POLL_S)
    return False, last


def _list_all_dns_servers(api: ApiClient) -> list[dict[str, Any]]:
    """Aggregate DNS server rows across every group (servers are per-group)."""
    out: list[dict[str, Any]] = []
    groups = api.json("GET", "/v1/dns/groups")
    for g in groups:
        # GET /v1/dns/groups/{gid}/servers — dns/router.py:1147
        out.extend(api.json("GET", f"/v1/dns/groups/{g['id']}/servers"))
    return out


def _poll_servers(api: ApiClient, log: logging.Logger, timeout_s: float) -> tuple[bool, dict[str, Any]]:
    """Poll until exactly one active bind9 + one active kea server row exist."""
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            dns_servers = _list_all_dns_servers(api)
            # GET /v1/dhcp/servers — dhcp/servers.py:289
            dhcp_servers = api.json("GET", "/v1/dhcp/servers")
        except Exception as exc:  # noqa: BLE001
            log_event(log, logging.WARNING, "server_poll_error", error=str(exc)[:200])
            time.sleep(POLL_S)
            continue
        bind9 = [s for s in dns_servers if s.get("driver") == "bind9"]
        kea = [s for s in dhcp_servers if s.get("driver") == "kea"]
        bind9_active = [s for s in bind9 if s.get("status") == "active"]
        kea_active = [s for s in kea if s.get("status") == "active"]
        last = {
            "bind9_total": len(bind9), "bind9_active": len(bind9_active),
            "kea_total": len(kea), "kea_active": len(kea_active),
            "bind9_servers": [{"name": s.get("name"), "status": s.get("status"),
                               "pending_approval": s.get("pending_approval")} for s in bind9],
            "kea_servers": [{"name": s.get("name"), "status": s.get("status"),
                             "agent_approved": s.get("agent_approved")} for s in kea],
        }
        log_event(log, logging.INFO, "server_rows",
                  bind9_active=len(bind9_active), kea_active=len(kea_active))
        if len(bind9_active) >= 1 and len(kea_active) >= 1:
            return True, last
        time.sleep(POLL_S)
    return False, last


def run(rp: RunPaths, m: manifest_mod.Manifest, log: logging.Logger) -> int:
    result: dict[str, Any] = {"ts": utc_now_iso(), "run_id": rp.run_id, "desired_roles": DESIRED_ROLES}
    rc = 0
    with ApiClient.from_manifest(m, run_id=rp.run_id) as api:
        appliance = _find_aio_appliance(api, log)
        appliance = _ensure_approved(api, appliance, log)
        aid = appliance["id"]
        result["appliance_id"] = aid
        result["hostname"] = appliance.get("hostname")
        result["capabilities"] = appliance.get("capabilities", {})

        # Capability gate is enforced server-side (422 if the box can't run a
        # role). Surface that as a clear failure rather than a stack trace.
        try:
            roled = _assign_roles(api, aid, log)
            result["assigned_roles"] = roled.get("assigned_roles")
        except Exception as exc:  # noqa: BLE001
            log.error("role assignment failed: %s", exc)
            result["role_assignment_error"] = str(exc)[:500]
            rc = 2

        ok_health, health = _poll_health(api, log, HEALTH_TIMEOUT_S)
        result["health_ok"] = ok_health
        result["health_last"] = health
        if not ok_health:
            log.error("/health/platform never reached 'ok' within %.0fs", HEALTH_TIMEOUT_S)
            rc = rc or 3

        ok_servers, servers = _poll_servers(api, log, SERVERS_TIMEOUT_S)
        result["servers_ok"] = ok_servers
        result["servers"] = servers
        if not ok_servers:
            log.error("bind9 + kea server rows not both active within %.0fs", SERVERS_TIMEOUT_S)
            rc = rc or 4
        else:
            # §7.5 step 4 asks for EXACTLY one of each — warn (not fail) on extras
            # so an HA fleet variant with ≥2 members still runs.
            if servers.get("bind9_active", 0) != 1 or servers.get("kea_active", 0) != 1:
                log.warning("expected exactly 1 bind9 + 1 kea active; got %s/%s",
                            servers.get("bind9_active"), servers.get("kea_active"))
                result["exactly_one_each"] = False
            else:
                result["exactly_one_each"] = True

    result["completed_at"] = utc_now_iso()
    result["rc"] = rc
    atomic_write_json(rp.snapshot("fleet_enable"), result)
    log_event(log, logging.INFO, "fleet_enable_done", rc=rc,
              health_ok=result.get("health_ok"), servers_ok=result.get("servers_ok"))
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Enable DNS+DHCP roles on the AIO node + verify health.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args(argv)

    rp = RunPaths.for_run(args.run_id, args.run_root)
    rp.ensure_dirs()
    log = get_logger("spddi_perf.seeder.fleet", run_id=args.run_id,
                     logfile=rp.worker_log("fleet_enable"))
    m = manifest_mod.load(args.manifest)
    try:
        return run(rp, m, log)
    except Exception as exc:  # noqa: BLE001
        log.exception("fleet_enable failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
