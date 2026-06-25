"""Minimal psql helper for the seeder one-shots (§5.4 direct-psql access).

The war-room psql probe is the heavy user of Postgres; the seeder only needs a
handful of one-off statements (CREATE EXTENSION, SHOW max_connections,
SHOW shared_preload_libraries). To avoid a hard dependency we try, in order:

  1. ``psycopg`` (v3, ``psycopg[binary]`` from perf/requirements.txt), then
  2. the ``psql`` CLI on PATH.

The DSN is read by the caller from the env var NAMED in the manifest
(``observability.psql_dsn_env``, default ``SPDDI_PERF_PSQL_DSN``) — never hard-coded
(non-negotiable #6). These run OFF-BOX against the appliance's exposed Postgres.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any


class PsqlUnavailable(RuntimeError):
    """Neither psycopg nor the psql CLI is available on this load-gen box."""


def _have_psycopg() -> bool:
    try:
        import psycopg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def backend() -> str:
    if _have_psycopg():
        return "psycopg"
    if shutil.which("psql"):
        return "psql-cli"
    return "none"


def execute(dsn: str, sql: str) -> None:
    """Run a statement with no result (e.g. CREATE EXTENSION). Autocommit."""
    if _have_psycopg():
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        return
    if shutil.which("psql"):
        _psql_cli(dsn, sql)
        return
    raise PsqlUnavailable("install psycopg[binary] (perf/requirements.txt) or the psql CLI")


def scalar(dsn: str, sql: str) -> Any:
    """Run a single-value query (e.g. ``SHOW max_connections``) → first cell."""
    if _have_psycopg():
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return row[0] if row else None
    if shutil.which("psql"):
        out = _psql_cli(dsn, sql, tuples_only=True)
        return out.strip() or None
    raise PsqlUnavailable("install psycopg[binary] (perf/requirements.txt) or the psql CLI")


def _psql_cli(dsn: str, sql: str, *, tuples_only: bool = False) -> str:
    argv = ["psql", dsn, "-v", "ON_ERROR_STOP=1"]
    if tuples_only:
        argv += ["-tA"]
    argv += ["-c", sql]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc.stdout
