#!/usr/bin/env python3
"""phase0_verify — the §9 Phase-0 unknown-confirmation gate (WARN-ONLY, §7.5 step 5).

Confirms + records the load-bearing unknowns before any load is offered. NEVER
hard-fails the run (the controller treats a non-zero rc as a warning only —
controller.py:124-126); the operator reads the PASS/WARN summary and decides.

Checks:
  1. Kea ``renew-timer: 900`` — the steady renewal floor is D_online/900, NOT
     lease/2 (render_kea.py:641 hardcodes it). Best-effort: rendered-config API,
     then optional ``kubectl exec`` read of the on-disk kea-dhcp4.conf.
  2. BIND9 ``recursion no;`` — authoritative-only safety (bind9.py:251, driven by
     group ``is_recursive=False``). Source: the agent-pushed named.conf snapshot.
  3. CNPG ``max_connections`` — verified 200 (NOT 100); the a1 connection budget
     rests on it (§5.1). Read via psql ``SHOW max_connections``.
  4. ``dns_record`` HARD-delete — the revoke path uses ``await db.delete(record)``
     despite the SoftDeleteMixin, so bloat math treats revoke as a heap DELETE
     (§5.1 / H6). Confirmed by code citation (a static fact, not runtime-observable
     off-box).

Writes ``snapshots/phase0.json`` + a per-check PASS/WARN summary.

GROUNDING (real backend / agent — cited inline):
  * GET /v1/dhcp/servers + /{id}/rendered-config — dhcp/servers.py:289,954.
  * GET /v1/dns/groups/{gid}/servers + /v1/dns/servers/{id}/rendered-config
    — dns/router.py:1147,3292 (agent-pushed file list incl. named.conf).
  * Kea renew-timer:900 hardcoded — agent/dhcp/.../render_kea.py:641
    (= spddi_perf.canonical.T1_RENEW_S).
  * BIND9 recursion render — agent/dns/.../drivers/bind9.py:251.
  * dns_record hard-delete — backend/app/api/v1/ipam/router.py:1066,1171,1353
    + dns/router.py:4466 (`await db.delete(record)`); model carries
    SoftDeleteMixin — backend/app/models/dns.py:743.

Usage:  python3 phase0_verify.py --run-id <id> --run-root <path> --manifest <path>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any

import spddi_perf.manifest as manifest_mod
from spddi_perf import canonical
from spddi_perf.logging_util import atomic_write_json, get_logger, log_event, utc_now_iso
from spddi_perf.runpaths import RunPaths

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _psql  # noqa: E402
from _api import ApiClient, ApiError  # noqa: E402

PASS, WARN, SKIP = "PASS", "WARN", "SKIP"

# Static code citation for the dns_record hard-delete fact (§5.1 / H6).
DNS_RECORD_HARD_DELETE_CITE = (
    "_sync_dns_record uses `await db.delete(record)` (ipam/router.py:1066,1171,1353) "
    "and the record-delete endpoint too (dns/router.py:4466) — a HARD heap DELETE "
    "despite DNSRecord carrying SoftDeleteMixin (models/dns.py:743). So revoke = "
    "DELETE for the dead-tuple/bloat math (verified)."
)


def _check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **extra}


# --- 1) Kea renew-timer:900 -----------------------------------------------------
def _verify_renew_timer(api: ApiClient, kubectl: str, ns: str, log: logging.Logger) -> dict[str, Any]:
    want = canonical.T1_RENEW_S  # 900
    # Try the API rendered-config first (note: the backend driver render may NOT
    # include renew-timer — that's added by the agent's render_kea — so this is a
    # best-effort look; the on-disk read below is authoritative).
    try:
        servers = api.json("GET", "/v1/dhcp/servers")
    except Exception as exc:  # noqa: BLE001
        return _check("kea_renew_timer", SKIP,
                      f"could not list DHCP servers ({str(exc)[:120]}); "
                      f"renew-timer={want} is hardcoded at render_kea.py:641",
                      expected=want)
    kea = [s for s in servers if s.get("driver") == "kea"]
    if not kea:
        return _check("kea_renew_timer", SKIP,
                      f"no kea server registered yet; renew-timer={want} hardcoded "
                      f"(render_kea.py:641 = canonical.T1_RENEW_S)", expected=want)

    sid = kea[0]["id"]
    found: int | None = None
    src = None
    # API render (may lack the key)
    try:
        cfg = api.json("GET", f"/v1/dhcp/servers/{sid}/rendered-config").get("config", "")
        found = _renew_from_kea_text(cfg)
        if found is not None:
            src = "api-rendered-config"
    except Exception as exc:  # noqa: BLE001
        log_event(log, logging.INFO, "kea_api_render_skip", error=str(exc)[:120])

    # On-disk read via kubectl exec (authoritative — that's the actual agent render).
    if found is None and (shutil.which(kubectl) or "/" in kubectl):
        text = _kubectl_read_kea(kubectl, ns, log)
        if text:
            found = _renew_from_kea_text(text)
            if found is not None:
                src = "kubectl-exec-on-disk"

    if found is None:
        return _check("kea_renew_timer", WARN,
                      f"renew-timer not observable off-box (API render lacks it; "
                      f"on-disk read unavailable). Hardcoded {want} at render_kea.py:641.",
                      expected=want)
    status = PASS if found == want else WARN
    return _check("kea_renew_timer", status,
                  f"renew-timer={found} (expected {want}) via {src}",
                  expected=want, found=found, source=src)


def _renew_from_kea_text(text: str) -> int | None:
    if not text:
        return None
    # Kea config is JSON; parse and walk Dhcp4.renew-timer if present.
    try:
        obj = json.loads(text)
        dh = obj.get("Dhcp4") or obj.get("Dhcp6") or obj
        v = dh.get("renew-timer")
        if isinstance(v, (int, float)):
            return int(v)
    except Exception:  # noqa: BLE001 — fall through to a substring scan
        pass
    # Substring fallback for non-JSON / partial dumps.
    import re
    m = re.search(r'"renew-timer"\s*:\s*(\d+)', text)
    return int(m.group(1)) if m else None


def _kubectl_read_kea(kubectl: str, ns: str, log: logging.Logger) -> str | None:
    """Best-effort read of /etc/kea/kea-dhcp4.conf from a kea pod (k8s topology)."""
    sel = "app.kubernetes.io/component=dhcp-kea"
    get = subprocess.run(
        [kubectl, "-n", ns, "get", "pods", "-l", sel,
         "--field-selector=status.phase=Running",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, timeout=30)
    pod = get.stdout.strip()
    if get.returncode != 0 or not pod:
        log_event(log, logging.INFO, "kea_pod_not_found", stderr=get.stderr.strip()[:200])
        return None
    cat = subprocess.run(
        [kubectl, "-n", ns, "exec", pod, "--", "cat", "/etc/kea/kea-dhcp4.conf"],
        capture_output=True, text=True, timeout=30)
    if cat.returncode != 0:
        log_event(log, logging.INFO, "kea_cat_failed", stderr=cat.stderr.strip()[:200])
        return None
    return cat.stdout


# --- 2) BIND9 recursion no; -----------------------------------------------------
def _verify_recursion(api: ApiClient, log: logging.Logger) -> dict[str, Any]:
    # Enumerate DNS servers across groups; read the agent-pushed named.conf.
    try:
        groups = api.json("GET", "/v1/dns/groups")
    except Exception as exc:  # noqa: BLE001
        return _check("bind9_recursion_off", SKIP,
                      f"could not list DNS groups ({str(exc)[:120]})")
    bind9_servers: list[tuple[str, str]] = []  # (group_id, server_id)
    for g in groups:
        try:
            for s in api.json("GET", f"/v1/dns/groups/{g['id']}/servers"):
                if s.get("driver") == "bind9":
                    bind9_servers.append((g["id"], s["id"]))
        except Exception:  # noqa: BLE001
            continue
    if not bind9_servers:
        return _check("bind9_recursion_off", SKIP,
                      "no bind9 server registered yet; recursion is driven by group "
                      "is_recursive=False (bind9.py:251)")
    _gid, sid = bind9_servers[0]
    try:
        # GET /v1/dns/servers/{id}/rendered-config — dns/router.py:3292
        resp = api.json("GET", f"/v1/dns/servers/{sid}/rendered-config")
    except ApiError as exc:
        return _check("bind9_recursion_off", WARN,
                      f"rendered-config fetch failed ({exc.status_code}); "
                      "recursion off is set by is_recursive=False")
    files = resp.get("files", [])
    named = next((f for f in files if "named.conf" in f.get("path", "")), None)
    if named is None or not resp.get("rendered_at"):
        return _check("bind9_recursion_off", WARN,
                      "no named.conf snapshot pushed yet (agent never reloaded); "
                      "recursion off is set by is_recursive=False (bind9.py:251)")
    content = named.get("content", "")
    has_recursion_no = "recursion no;" in content
    has_recursion_yes = "recursion yes;" in content
    if has_recursion_no and not has_recursion_yes:
        return _check("bind9_recursion_off", PASS,
                      "named.conf contains 'recursion no;' (authoritative-only)")
    if has_recursion_yes:
        return _check("bind9_recursion_off", WARN,
                      "named.conf contains 'recursion yes;' — DNS SAFETY RISK (§4.9); "
                      "set the DNS group is_recursive=False",
                      danger=True)
    return _check("bind9_recursion_off", WARN,
                  "named.conf has no explicit 'recursion no;' directive — verify §4.9")


# --- 3) max_connections ---------------------------------------------------------
def _verify_max_connections(m: manifest_mod.Manifest, log: logging.Logger) -> dict[str, Any]:
    dsn = os.environ.get(m.observability.psql_dsn_env)
    if not dsn:
        return _check("pg_max_connections", SKIP,
                      f"${m.observability.psql_dsn_env} not set")
    if _psql.backend() == "none":
        return _check("pg_max_connections", SKIP,
                      "no psycopg / psql available on this box")
    try:
        val = _psql.scalar(dsn, "SHOW max_connections;")
    except Exception as exc:  # noqa: BLE001
        return _check("pg_max_connections", WARN, f"SHOW failed: {str(exc)[:200]}")
    try:
        n = int(str(val))
    except (TypeError, ValueError):
        return _check("pg_max_connections", WARN, f"unparseable max_connections={val!r}")
    # §5.1 verified 200 on CNPG. Record the real number; WARN if surprisingly low.
    status = PASS if n >= 100 else WARN
    note = f"max_connections={n}"
    if n != 200:
        note += " (§5.1 expected 200 on CNPG — recompute the a1 conn budget if different)"
    return _check("pg_max_connections", status, note, found=n, expected=200)


# --- 4) dns_record hard-delete (static code confirmation) -----------------------
def _verify_dns_record_hard_delete() -> dict[str, Any]:
    return _check("dns_record_hard_delete", PASS, DNS_RECORD_HARD_DELETE_CITE)


def run(rp: RunPaths, m: manifest_mod.Manifest, log: logging.Logger,
        kubectl: str, ns: str) -> int:
    checks: list[dict[str, Any]] = []
    # dns_record check is pure code-citation; the others need the API.
    checks.append(_verify_dns_record_hard_delete())
    checks.append(_verify_max_connections(m, log))
    try:
        with ApiClient.from_manifest(m, run_id=rp.run_id) as api:
            checks.append(_verify_renew_timer(api, kubectl, ns, log))
            checks.append(_verify_recursion(api, log))
    except Exception as exc:  # noqa: BLE001 — API unreachable → record, don't crash
        checks.append(_check("api_reachable", WARN, f"API unreachable: {str(exc)[:200]}"))

    n_pass = sum(1 for c in checks if c["status"] == PASS)
    n_warn = sum(1 for c in checks if c["status"] == WARN)
    n_skip = sum(1 for c in checks if c["status"] == SKIP)
    danger = any(c.get("danger") for c in checks)
    summary = {
        "ts": utc_now_iso(),
        "run_id": rp.run_id,
        "phase": "phase0_verify",
        "warn_only": True,
        "topology": {"dhcp": m.target.dhcp.topology, "dns_driver": m.target.dns.driver},
        "totals": {"pass": n_pass, "warn": n_warn, "skip": n_skip, "danger": danger},
        "checks": checks,
        "canonical": {"T1_RENEW_S": canonical.T1_RENEW_S, "T2_REBIND_S": canonical.T2_REBIND_S},
    }
    atomic_write_json(rp.snapshot("phase0"), summary)
    for c in checks:
        log_event(log, logging.INFO, "phase0_check",
                  check=c["check"], status=c["status"], detail=c["detail"][:300])
    log_event(log, logging.INFO, "phase0_summary",
              passed=n_pass, warned=n_warn, skipped=n_skip, danger=danger)
    # Warn-only: return 0 unless a real DNS-safety danger surfaced (recursion yes),
    # which is worth a non-zero so the controller logs a phase0_verify_warning.
    return 10 if danger else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase-0 unknown-confirmation gate (warn-only).")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--namespace",
                    default=os.environ.get("SPDDI_PERF_K8S_NAMESPACE", "spatiumddi"))
    ap.add_argument("--kubectl", default=os.environ.get("SPDDI_PERF_KUBECTL", "kubectl"))
    args = ap.parse_args(argv)

    rp = RunPaths.for_run(args.run_id, args.run_root)
    rp.ensure_dirs()
    log = get_logger("spddi_perf.seeder.phase0", run_id=args.run_id,
                     logfile=rp.worker_log("phase0_verify"))
    m = manifest_mod.load(args.manifest)
    try:
        return run(rp, m, log, args.kubectl, args.namespace)
    except Exception as exc:  # noqa: BLE001 — phase0 must never crash the provision step
        log.exception("phase0_verify error (non-fatal): %s", exc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
