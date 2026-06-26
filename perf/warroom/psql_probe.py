#!/usr/bin/env python3
"""Direct-psql war-room probe (long-running, off-box) — the authoritative source for
the metrics no native endpoint exposes (§5.4 / §8.6): locks, deadlocks, WAL rate,
per-table tuple churn, vacuum progress, and the §8.2.4 domain-truth ledger.

The native ``/admin/postgres/*`` rollups go through the api pool + CNPG, so this
probe is the *single source* for a4 (deadlocks) / a5 (lock-wait waiters) / a8
(``dns_zone`` hot-row contention) — it connects with its own dedicated DSN (ideally
a read-only ``pg_monitor`` role) whose 1–2 conns are baselined out of the a1 budget.

Surfaces (cadence ``psql_locks_s`` from manifest.observability.poll, default 30s):
  warroom/pg_locks.ndjson        : pg_locks ⋈ pg_stat_activity — advisory waiters (H1)
                                   + relation/tuple waits on dns_zone/ip_address/
                                   dhcp_lease (H3/H8), deadlocks + commit/rollback/
                                   temp_bytes from pg_stat_database, WAL rate (lsn delta)
  warroom/pg_activity.ndjson     : pg_stat_activity by state/wait_event (xact_start age)
                                   + pg_stat_progress_vacuum rows
  warroom/pg_user_tables.ndjson  : pg_stat_user_tables for the §5.4 focus set
  warroom/domain_counts.ndjson   : the §8.2.4 propagation-completeness ledger (EXACT
                                   shape the watchdog + drain-convergence check read)

Secrets: DSN from the env var NAMED in the manifest
(``observability.psql_dsn_env``, default ``SPDDI_PERF_PSQL_DSN``). NEVER hardcoded.

Stops cleanly on kill-switch (``rp.stop_file``), stale setpoint (controller gone),
SIGTERM/SIGINT.

Grounding (table + column names verified against the live models):
  * dhcp_lease (state, ip_address)            backend/app/models/dhcp.py:633-660
  * dhcp_lease_history                         backend/app/models/dhcp.py:705-731
  * ip_address (auto_from_lease)               backend/app/models/ipam.py:771,860
  * dns_record (deleted_at via SoftDeleteMixin) backend/app/models/dns.py:743 + base.py:40-53
  * dns_record_op (state default 'pending')    backend/app/models/dns.py:317,329
  * dns_zone / dns_server_zone_state           backend/app/models/dns.py:585,46
  * dns_query_log_entry / dhcp_log_entry       backend/app/models/logs.py:48,83
  * audit_log                                  backend/app/models/audit.py:28
  * focus-set list                             docs/PERFORMANCE_TESTING.md §5.4 (1211-1213)
  * lease state 'active' / record_op 'pending' models above (defaults)
"""

from __future__ import annotations

import argparse
import os
import signal
import time
from typing import Any

import spddi_perf.manifest
import spddi_perf.setpoints
from spddi_perf.logging_util import append_ndjson, get_logger, log_event
from spddi_perf.runpaths import RunPaths

# §5.4 focus set — the tables whose ins/upd/del + dead_tup + autovacuum we watch.
FOCUS_TABLES = (
    "dhcp_lease",
    "ip_address",
    "dhcp_lease_history",
    "dns_record",
    "dns_record_op",
    "dns_zone",
    "dns_query_log_entry",
    "dhcp_log_entry",
    "audit_log",
    "dns_server_zone_state",
)

# Relation/tuple-lock targets for H3/H8 (hot-row contention).
LOCK_WATCH_TABLES = ("dns_zone", "ip_address", "dhcp_lease")

DEFAULT_LOCKS_CADENCE_S = 30.0
STALE_SETPOINT_S = 200.0


class _Stop:
    def __init__(self) -> None:
        self.flag = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, *_: Any) -> None:
        self.flag = True


def _cadence(m: spddi_perf.manifest.Manifest) -> float:
    raw = (m.observability.poll or {}).get("psql_locks_s", DEFAULT_LOCKS_CADENCE_S)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_LOCKS_CADENCE_S


# ── SQL (all read-only; parameterised where a value is interpolated) ──────────

# pg_locks ⋈ pg_stat_activity: every ungranted lock waiter (a5) + advisory locks
# (H1 audit chain) + relation/tuple waits on the hot tables (H3/H8). Self-excluded.
_SQL_LOCK_WAITERS = """
SELECT l.locktype,
       l.mode,
       l.granted,
       CASE WHEN c.relname IS NOT NULL THEN c.relname ELSE NULL END AS relation,
       a.state,
       a.wait_event_type,
       a.wait_event,
       EXTRACT(EPOCH FROM (now() - a.xact_start))::float AS xact_age_s,
       a.pid
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
LEFT JOIN pg_class c ON c.oid = l.relation
WHERE a.datname = current_database()
  AND a.pid <> pg_backend_pid()
  AND (l.granted = false OR l.locktype = 'advisory'
       OR c.relname = ANY(:watch))
"""

# pg_stat_database for this DB — deadlocks (a4), commit/rollback rate, temp spill (a9).
_SQL_PG_DATABASE = """
SELECT xact_commit, xact_rollback, deadlocks,
       blks_hit, blks_read, temp_files, temp_bytes
FROM pg_stat_database
WHERE datname = current_database()
"""

# WAL position (primary only; replicas raise). delta across samples = WAL rate (a10).
_SQL_WAL_LSN = "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint"

# pg_stat_activity grouped by state + wait_event — the by-state + wait breakdown.
_SQL_ACTIVITY = """
SELECT COALESCE(state, 'unknown') AS state,
       COALESCE(wait_event_type, '') AS wait_event_type,
       COALESCE(wait_event, '') AS wait_event,
       count(*) AS n,
       max(EXTRACT(EPOCH FROM (now() - xact_start)))::float AS max_xact_age_s
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
GROUP BY 1, 2, 3
"""

# pg_stat_progress_vacuum — live (auto)vacuum work, incl. on the focus tables.
_SQL_VACUUM_PROGRESS = """
SELECT v.pid,
       c.relname,
       v.phase,
       v.heap_blks_total,
       v.heap_blks_scanned,
       v.heap_blks_vacuumed,
       v.num_dead_tuples
FROM pg_stat_progress_vacuum v
LEFT JOIN pg_class c ON c.oid = v.relid
"""

# pg_stat_user_tables for the focus set — tuple churn + autovacuum bookkeeping (a7).
_SQL_USER_TABLES = """
SELECT relname,
       n_live_tup, n_dead_tup,
       n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd,
       vacuum_count, autovacuum_count, analyze_count, autoanalyze_count,
       EXTRACT(EPOCH FROM (now() - last_autovacuum))::float AS last_autovacuum_age_s,
       EXTRACT(EPOCH FROM (now() - last_autoanalyze))::float AS last_autoanalyze_age_s
FROM pg_stat_user_tables
WHERE relname = ANY(:focus)
"""

# §8.2.4 domain-truth ledger. Each count grounded on the model above. Single
# round-trip via a CTE so the bracket is internally consistent (one snapshot).
_SQL_DOMAIN_COUNTS = """
SELECT
  (SELECT count(*) FROM dns_record_op WHERE state = 'pending')           AS dns_record_op_pending,
  (SELECT count(*) FROM dns_record_op)                                   AS dns_record_op_total,
  (SELECT count(*) FROM dhcp_lease WHERE state = 'active')               AS active_leases,
  (SELECT count(*) FROM ip_address WHERE auto_from_lease)               AS ipam_mirror,
  (SELECT count(*) FROM dns_record WHERE deleted_at IS NULL)             AS dns_records,
  (SELECT count(*) FROM audit_log)                                       AS audit_rows,
  (SELECT count(*) FROM dhcp_lease)                                      AS dhcp_lease_total,
  (SELECT count(*) FROM dhcp_lease_history)                              AS dhcp_lease_history
"""


def _connect(dsn: str):
    import psycopg  # psycopg 3.x

    # autocommit so each catalog read is its own short txn (never an open xact that
    # would itself show up as idle-in-transaction in the very stats we're measuring).
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5,
                           application_name="spddi-perf-psql-probe")
    return conn


def _rows(cur) -> list[tuple]:
    try:
        return cur.fetchall()
    except Exception:  # noqa: BLE001
        return []


class Probe:
    def __init__(self, rp: RunPaths, dsn: str, log) -> None:
        self.rp = rp
        self.dsn = dsn
        self.log = log
        self.conn = None
        self._last_wal: int | None = None
        self._last_wal_t: float | None = None

    def _ensure_conn(self) -> bool:
        if self.conn is not None:
            try:
                if not self.conn.closed:
                    return True
            except Exception:  # noqa: BLE001
                pass
        try:
            self.conn = _connect(self.dsn)
            log_event(self.log, 20, "psql_connected")
            return True
        except Exception as exc:  # noqa: BLE001 — degrade, retry next tick
            self.conn = None
            log_event(self.log, 30, "psql_connect_failed", error=str(exc))
            return False

    def _exec(self, sql: str, params: dict | None = None) -> list[tuple] | None:
        if not self._ensure_conn():
            return None
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params or {})
                return _rows(cur)
        except Exception as exc:  # noqa: BLE001
            # First non-blank line as a label — robust for single-line SQL too
            # (the old sql.split("\n")[1] raised IndexError on _SQL_WAL_LSN).
            label = next((ln.strip() for ln in sql.splitlines() if ln.strip()), sql.strip())
            log_event(self.log, 20, "psql_query_failed", error=str(exc), sql=label[:80])
            # Drop the connection so the next tick reconnects (covers failover).
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
            return None

    # ── pg_locks.ndjson ──────────────────────────────────────────────────────
    def probe_locks(self) -> None:
        waiters = self._exec(_SQL_LOCK_WAITERS, {"watch": list(LOCK_WATCH_TABLES)})
        dbrow = self._exec(_SQL_PG_DATABASE)
        wal = self._exec(_SQL_WAL_LSN)

        if waiters is None and dbrow is None and wal is None:
            append_ndjson(self.rp.warroom("pg_locks"), {"available": False,
                          "error": "psql unavailable"})
            return

        lock_records: list[dict[str, Any]] = []
        n_waiting = 0
        advisory_waiting = 0
        relation_waiting: dict[str, int] = {}
        for r in waiters or []:
            granted = bool(r[2])
            rec = {
                "locktype": r[0], "mode": r[1], "granted": granted,
                "relation": r[3], "state": r[4],
                "wait_event_type": r[5], "wait_event": r[6],
                "xact_age_s": float(r[7]) if r[7] is not None else None, "pid": r[8],
            }
            lock_records.append(rec)
            if not granted:
                n_waiting += 1
                if r[0] == "advisory":
                    advisory_waiting += 1
                if r[3]:
                    relation_waiting[r[3]] = relation_waiting.get(r[3], 0) + 1

        out: dict[str, Any] = {
            "available": True,
            "locks_waiting": n_waiting,          # a5: any sustained > 0 = lock-wait
            "advisory_waiting": advisory_waiting,  # H1: audit-chain advisory contention
            "relation_waiting": relation_waiting,  # H3/H8: per-hot-table waits
            "locks": lock_records,
        }
        if dbrow:
            d = dbrow[0]
            out["pg_database"] = {
                "xact_commit": int(d[0] or 0),
                "xact_rollback": int(d[1] or 0),
                "deadlocks": int(d[2] or 0),      # a4: any increment = FAIL
                "blks_hit": int(d[3] or 0),
                "blks_read": int(d[4] or 0),
                "temp_files": int(d[5] or 0),
                "temp_bytes": int(d[6] or 0),     # a9: work_mem spill
            }
        # WAL rate (a10): bytes/sec since the last sample (None on first sample/replica).
        if wal and wal[0] and wal[0][0] is not None:
            cur_lsn = int(wal[0][0])
            now = time.monotonic()
            if self._last_wal is not None and self._last_wal_t is not None:
                dt = now - self._last_wal_t
                if dt > 0:
                    out["wal_bytes_per_s"] = max(0.0, (cur_lsn - self._last_wal) / dt)
            out["wal_lsn_bytes"] = cur_lsn
            self._last_wal = cur_lsn
            self._last_wal_t = now
        append_ndjson(self.rp.warroom("pg_locks"), out)

    # ── pg_activity.ndjson ───────────────────────────────────────────────────
    def probe_activity(self) -> None:
        act = self._exec(_SQL_ACTIVITY)
        vac = self._exec(_SQL_VACUUM_PROGRESS)
        if act is None and vac is None:
            append_ndjson(self.rp.warroom("pg_activity"), {"available": False,
                          "error": "psql unavailable"})
            return
        by_state: list[dict[str, Any]] = []
        for r in act or []:
            by_state.append({
                "state": r[0], "wait_event_type": r[1], "wait_event": r[2],
                "n": int(r[3] or 0),
                "max_xact_age_s": float(r[4]) if r[4] is not None else None,
            })
        vacuums: list[dict[str, Any]] = []
        for r in vac or []:
            vacuums.append({
                "pid": r[0], "relation": r[1], "phase": r[2],
                "heap_blks_total": int(r[3] or 0),
                "heap_blks_scanned": int(r[4] or 0),
                "heap_blks_vacuumed": int(r[5] or 0),
                "num_dead_tuples": int(r[6] or 0),
            })
        append_ndjson(self.rp.warroom("pg_activity"),
                      {"available": True, "by_state": by_state, "vacuum_progress": vacuums})

    # ── pg_user_tables.ndjson ────────────────────────────────────────────────
    def probe_user_tables(self) -> None:
        rows = self._exec(_SQL_USER_TABLES, {"focus": list(FOCUS_TABLES)})
        if rows is None:
            append_ndjson(self.rp.warroom("pg_user_tables"), {"available": False,
                          "error": "psql unavailable"})
            return
        tables: dict[str, dict[str, Any]] = {}
        for r in rows:
            tables[r[0]] = {
                "live_tup": int(r[1] or 0),
                "dead_tup": int(r[2] or 0),
                "n_tup_ins": int(r[3] or 0),
                "n_tup_upd": int(r[4] or 0),
                "n_tup_del": int(r[5] or 0),
                "n_tup_hot_upd": int(r[6] or 0),
                "vacuum_count": int(r[7] or 0),
                "autovacuum_count": int(r[8] or 0),
                "analyze_count": int(r[9] or 0),
                "autoanalyze_count": int(r[10] or 0),
                "last_autovacuum_age_s": float(r[11]) if r[11] is not None else -1.0,
                "last_autoanalyze_age_s": float(r[12]) if r[12] is not None else -1.0,
            }
        append_ndjson(self.rp.warroom("pg_user_tables"), {"available": True, "tables": tables})

    # ── domain_counts.ndjson (§8.2.4 ledger) ─────────────────────────────────
    def probe_domain_counts(self) -> None:
        rows = self._exec(_SQL_DOMAIN_COUNTS)
        if not rows:
            append_ndjson(self.rp.warroom("domain_counts"), {"available": False,
                          "error": "psql unavailable"})
            return
        r = rows[0]
        # EXACT shape per the contract — every key required, every value an int.
        append_ndjson(self.rp.warroom("domain_counts"), {
            "available": True,
            "dns_record_op_pending": int(r[0] or 0),
            "dns_record_op_total": int(r[1] or 0),
            "active_leases": int(r[2] or 0),
            "ipam_mirror": int(r[3] or 0),
            "dns_records": int(r[4] or 0),
            "audit_rows": int(r[5] or 0),
            "dhcp_lease_total": int(r[6] or 0),
            "dhcp_lease_history": int(r[7] or 0),
        })

    def tick(self) -> None:
        self.probe_locks()
        self.probe_activity()
        self.probe_user_tables()
        self.probe_domain_counts()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass


def _setpoint_stale(rp: RunPaths, last_tick: int, last_seen_at: float) -> tuple[int, float, bool]:
    sp = spddi_perf.setpoints.read_current(rp)
    now = time.monotonic()
    if sp is None:
        return last_tick, last_seen_at, False
    if sp.tick != last_tick:
        return sp.tick, now, False
    return last_tick, last_seen_at, (now - last_seen_at) > STALE_SETPOINT_S


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Direct-psql war-room probe (off-box). The authoritative source "
                    "for locks/deadlocks/WAL-rate/vacuum + the §8.2.4 domain-truth "
                    "ledger that the native API does not expose. Appends NDJSON to "
                    "warroom/{pg_locks,pg_activity,pg_user_tables,domain_counts}.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args(argv)

    rp = RunPaths.for_run(args.run_id, args.run_root)
    m = spddi_perf.manifest.load(args.manifest)
    log = get_logger("warroom.psql_probe", service="spddi-perf-warroom",
                     run_id=args.run_id, logfile=rp.worker_log("psql_probe"))

    dsn_env = m.observability.psql_dsn_env
    dsn = os.environ.get(dsn_env, "")
    if not dsn:
        log_event(log, 40, "psql_dsn_missing", env=dsn_env,
                  note="cannot probe locks/deadlocks/domain-counts without the DSN")
        # Still record an unavailable bracket on every surface so the report can
        # explain the gap, then exit non-fatally-empty.
        for surface in ("pg_locks", "pg_activity", "pg_user_tables", "domain_counts"):
            append_ndjson(rp.warroom(surface), {"available": False,
                          "error": f"{dsn_env} not set"})
        return 2

    cadence = _cadence(m)
    log_event(log, 20, "psql_probe_start", cadence_s=cadence, dsn_env=dsn_env,
              focus_tables=list(FOCUS_TABLES))

    stop = _Stop()
    probe = Probe(rp, dsn, log)
    last_tick = -1
    last_seen_at = time.monotonic()
    stale_logged = False
    next_due = time.monotonic()
    try:
        while not stop.flag:
            if rp.stop_file.exists():
                log_event(log, 20, "kill_switch_seen", path=str(rp.stop_file))
                break

            last_tick, last_seen_at, stale = _setpoint_stale(rp, last_tick, last_seen_at)
            if stale and not stale_logged:
                # Monitor fail-safe: keep observing (capture the box dying) but flag
                # the controller is gone. Back off the cadence to 2× to be gentle on
                # the SUT once nobody's driving load (§5.4 observer discipline).
                log_event(log, 30, "setpoint_lag_controller_gone", last_tick=last_tick,
                          note="controller stale > %.0fs — continuing at 2x cadence" % STALE_SETPOINT_S)
                stale_logged = True
            elif not stale:
                stale_logged = False

            now = time.monotonic()
            if now >= next_due:
                probe.tick()
                eff_cadence = cadence * 2.0 if stale else cadence
                next_due = now + eff_cadence

            time.sleep(min(1.0, max(0.2, next_due - time.monotonic())))
    finally:
        probe.close()
        log_event(log, 20, "psql_probe_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
