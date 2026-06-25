#!/usr/bin/env python3
"""pgss_enable — ensure pg_stat_statements is live before any load (§5.1, §5.4, §7.5).

``pg_stat_statements`` is OFF by default on CNPG (verified — admin/postgres.py:327
returns ``available=false`` until the extension is created, and the extension itself
needs ``shared_preload_libraries`` to contain ``pg_stat_statements``, which requires
a Postgres restart = provisioning).

This one-shot:

  1. Runs ``CREATE EXTENSION IF NOT EXISTS pg_stat_statements`` over the psql DSN env.
  2. Asserts availability via the admin slow-queries endpoint (``available: true``).
  3. If ``shared_preload_libraries`` lacks ``pg_stat_statements`` (CREATE EXTENSION
     fails / availability stays false), emits a clear, copy-pasteable kubectl/psql
     remediation and exits NON-ZERO. The controller treats provision warnings as
     non-fatal (controller.py:127 only logs), so a missing-preload box doesn't abort
     the run — but the operator sees exactly what to fix.

GROUNDING (real backend / infra — cited inline):
  * pg_stat_statements OFF by default; needs shared_preload_libraries + restart +
    CREATE EXTENSION — admin/postgres.py:327-345 (the hint text the endpoint returns).
  * GET /v1/admin/postgres/slow-queries → {available: bool, hint?, rows?}
    — admin/postgres.py:321 (SuperAdmin-gated; prefix /admin: router.py:103).
  * CNPG Cluster CR name ``<release>-postgresql`` w/ spec.postgresql.parameters —
    charts/spatiumddi/templates/cnpg-cluster.yaml:4,82.

Usage:  python3 pgss_enable.py --run-id <id> --run-root <path> --manifest <path>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

import spddi_perf.manifest as manifest_mod
from spddi_perf.logging_util import atomic_write_json, get_logger, log_event, utc_now_iso
from spddi_perf.runpaths import RunPaths

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _psql  # noqa: E402
from _api import ApiClient  # noqa: E402

# Copy-pasteable remediation when shared_preload_libraries lacks the extension.
# CNPG owns postgresql.conf, so the parameter goes on the Cluster CR; CNPG then
# does a rolling restart. The cluster name follows the chart convention.
_REMEDIATION = """\
pg_stat_statements is NOT in shared_preload_libraries — it cannot be CREATEd until
Postgres is restarted with the library preloaded. On the CNPG appliance:

  # 1) Add the library to the CNPG Cluster CR (triggers a rolling restart):
  kubectl -n spatiumddi patch cluster spatiumddi-postgresql --type merge -p \\
    '{"spec":{"postgresql":{"parameters":{"shared_preload_libraries":"pg_stat_statements","pg_stat_statements.max":"10000","pg_stat_statements.track":"all"}}}}'

  # 2) Wait for the rollout, then create the extension:
  kubectl -n spatiumddi wait --for=condition=Ready cluster/spatiumddi-postgresql --timeout=600s
  psql "$SPDDI_PERF_PSQL_DSN" -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements;'

(Adjust the namespace / cluster name to your install — chart convention is
'<release>-postgresql'. On a non-CNPG standalone Postgres, set
shared_preload_libraries in postgresql.conf and restart instead.)"""


def _slow_queries_available(api: ApiClient) -> tuple[bool, dict[str, Any]]:
    # GET /v1/admin/postgres/slow-queries — admin/postgres.py:321
    data = api.json("GET", "/v1/admin/postgres/slow-queries")
    return bool(data.get("available")), data


def run(rp: RunPaths, m: manifest_mod.Manifest, log: logging.Logger) -> int:
    result: dict[str, Any] = {"ts": utc_now_iso(), "run_id": rp.run_id}
    dsn_env = m.observability.psql_dsn_env
    dsn = os.environ.get(dsn_env)
    result["psql_backend"] = _psql.backend()

    # ---- 1) CREATE EXTENSION over the DSN ----
    create_ok = False
    create_err: str | None = None
    if not dsn:
        create_err = f"${dsn_env} not set — cannot CREATE EXTENSION"
        log.error(create_err)
    elif result["psql_backend"] == "none":
        create_err = "no psycopg / psql available on this box"
        log.error(create_err)
    else:
        try:
            _psql.execute(dsn, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;")
            create_ok = True
            log_event(log, logging.INFO, "create_extension_ok")
        except Exception as exc:  # noqa: BLE001 — likely the missing-preload case
            create_err = str(exc)[:500]
            log_event(log, logging.WARNING, "create_extension_failed", error=create_err)
    result["create_extension_ok"] = create_ok
    if create_err:
        result["create_extension_error"] = create_err

    # ---- best-effort: record the actual server limits (Phase-0 corroboration) ----
    if dsn and result["psql_backend"] != "none":
        for key, sql in (
            ("max_connections", "SHOW max_connections;"),
            ("shared_preload_libraries", "SHOW shared_preload_libraries;"),
        ):
            try:
                result[key] = _psql.scalar(dsn, sql)
            except Exception as exc:  # noqa: BLE001
                result[f"{key}_error"] = str(exc)[:200]

    preload = str(result.get("shared_preload_libraries") or "")
    result["preload_has_pgss"] = "pg_stat_statements" in preload

    # ---- 2) Assert availability via the API ----
    available = False
    api_data: dict[str, Any] = {}
    try:
        with ApiClient.from_manifest(m, run_id=rp.run_id) as api:
            available, api_data = _slow_queries_available(api)
    except Exception as exc:  # noqa: BLE001
        result["slow_queries_api_error"] = str(exc)[:500]
        log_event(log, logging.WARNING, "slow_queries_api_error", error=str(exc)[:200])
    result["slow_queries_available"] = available
    if api_data.get("hint"):
        result["slow_queries_hint"] = api_data["hint"]

    atomic_write_json(rp.snapshot("pgss_enable"), result)

    if available:
        log_event(log, logging.INFO, "pgss_enabled",
                  max_connections=result.get("max_connections"))
        return 0

    # ---- 3) Not available → remediation + non-zero (warn-only at controller) ----
    log_event(log, logging.ERROR, "pgss_unavailable",
              preload_has_pgss=result["preload_has_pgss"],
              create_extension_ok=create_ok)
    if not result["preload_has_pgss"]:
        # The actionable case: needs a CNPG restart with the library preloaded.
        for line in _REMEDIATION.splitlines():
            log.error("remediation: %s", line)
        result["remediation"] = _REMEDIATION
        atomic_write_json(rp.snapshot("pgss_enable"), result)
        return 5
    # Preload present but still unavailable → CREATE EXTENSION never ran / failed.
    log.error("shared_preload_libraries has pg_stat_statements but the extension is "
              "not available — re-run CREATE EXTENSION over $%s (error: %s)",
              dsn_env, create_err)
    return 6


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ensure pg_stat_statements is enabled pre-run.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args(argv)

    rp = RunPaths.for_run(args.run_id, args.run_root)
    rp.ensure_dirs()
    log = get_logger("spddi_perf.seeder.pgss", run_id=args.run_id,
                     logfile=rp.worker_log("pgss_enable"))
    m = manifest_mod.load(args.manifest)
    try:
        return run(rp, m, log)
    except Exception as exc:  # noqa: BLE001
        log.exception("pgss_enable failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
