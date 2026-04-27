"""Postgres insights — read-only diagnostic surface for the admin UI.

Exposes a small set of pg_stat / pg_*_size queries so operators can see
slow queries, table sizes, connection pool usage, cache hit rates, WAL
bloat, and the longest-running transaction without standing up a
separate Prometheus / pgwatch / Grafana pipeline. Native, ~ms latency,
no extra infra.

Every endpoint is read-only and superadmin-gated. ``pg_stat_statements``
is optional — if the extension isn't installed (most managed providers
ship it but a stock self-hosted Postgres won't) the slow-queries
endpoint reports a friendly "extension not available" hint rather than
500ing.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import DB, SuperAdmin

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Response shapes ───────────────────────────────────────────────────────


class SlowQueryRow(BaseModel):
    query: str
    calls: int
    total_time_ms: float
    mean_time_ms: float
    rows: int


class SlowQueriesResponse(BaseModel):
    available: bool
    hint: str | None = None
    rows: list[SlowQueryRow] = []


class TableSizeRow(BaseModel):
    schema_name: str
    table_name: str
    total_bytes: int
    table_bytes: int
    index_bytes: int
    toast_bytes: int
    live_rows: int
    dead_rows: int
    last_autovacuum: str | None
    last_autoanalyze: str | None


class ConnectionsRow(BaseModel):
    state: str
    count: int


class LongestTransactionRow(BaseModel):
    pid: int
    state: str | None
    age_seconds: float
    query: str | None
    application_name: str | None
    client_addr: str | None


class OverviewResponse(BaseModel):
    version: str
    db_size_bytes: int
    cache_hit_ratio: float | None  # 0..1, None if no reads yet
    wal_bytes: int | None  # current WAL position; None on replicas
    active_connections: int
    max_connections: int
    longest_transaction: LongestTransactionRow | None


class TableSizesResponse(BaseModel):
    rows: list[TableSizeRow]


class ConnectionsResponse(BaseModel):
    rows: list[ConnectionsRow]


# ── Helpers ───────────────────────────────────────────────────────────────


async def _setting_int(db, name: str) -> int | None:
    res = await db.execute(text(f"SHOW {name}"))
    row = res.first()
    if row is None:
        return None
    try:
        return int(row[0])
    except (ValueError, TypeError):
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/postgres/overview", response_model=OverviewResponse)
async def postgres_overview(db: DB, _: SuperAdmin) -> OverviewResponse:
    """One-shot dashboard rollup: version, DB size, cache hit, WAL, active conns,
    longest transaction. Cheap — every query is O(1) or close to it."""

    version_row = (await db.execute(text("SELECT version()"))).first()
    version = str(version_row[0]) if version_row else "unknown"

    size_row = (await db.execute(text("SELECT pg_database_size(current_database())"))).first()
    db_size = int(size_row[0]) if size_row else 0

    # Cache hit ratio across the whole DB. Heap-block hits / (hits+reads).
    hit_row = (
        await db.execute(
            text("SELECT sum(heap_blks_hit), sum(heap_blks_read) " "FROM pg_statio_user_tables")
        )
    ).first()
    if hit_row and hit_row[0] is not None:
        hit = int(hit_row[0] or 0)
        read = int(hit_row[1] or 0)
        cache_hit = (hit / (hit + read)) if (hit + read) > 0 else None
    else:
        cache_hit = None

    # WAL position — only on the primary; replicas raise "function returns NULL".
    wal_bytes: int | None = None
    try:
        wal_row = (
            await db.execute(text("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')"))
        ).first()
        wal_bytes = int(wal_row[0]) if wal_row and wal_row[0] is not None else None
    except Exception:
        wal_bytes = None

    active_row = (
        await db.execute(
            text("SELECT count(*) FROM pg_stat_activity " "WHERE datname = current_database()")
        )
    ).first()
    active_conns = int(active_row[0]) if active_row else 0
    max_conns = await _setting_int(db, "max_connections") or 0

    # Longest currently-running transaction.
    longest = None
    longest_row = (await db.execute(text("""
                SELECT pid, state,
                       EXTRACT(EPOCH FROM (now() - xact_start))::float AS age_s,
                       query, application_name, client_addr::text
                FROM pg_stat_activity
                WHERE xact_start IS NOT NULL
                  AND pid <> pg_backend_pid()
                  AND datname = current_database()
                ORDER BY xact_start ASC
                LIMIT 1
                """))).first()
    if longest_row:
        longest = LongestTransactionRow(
            pid=int(longest_row[0]),
            state=longest_row[1],
            age_seconds=float(longest_row[2] or 0),
            query=longest_row[3],
            application_name=longest_row[4],
            client_addr=longest_row[5],
        )

    return OverviewResponse(
        version=version,
        db_size_bytes=db_size,
        cache_hit_ratio=cache_hit,
        wal_bytes=wal_bytes,
        active_connections=active_conns,
        max_connections=max_conns,
        longest_transaction=longest,
    )


@router.get("/postgres/tables", response_model=TableSizesResponse)
async def postgres_table_sizes(
    db: DB,
    _: SuperAdmin,
    limit: int = Query(50, ge=1, le=500),
) -> TableSizesResponse:
    """Top tables by total size (table + indexes + TOAST). Useful for
    catching unbounded growth in audit / metrics / log tables."""

    res = await db.execute(
        text("""
            SELECT n.nspname,
                   c.relname,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   pg_relation_size(c.oid) AS table_bytes,
                   pg_indexes_size(c.oid) AS index_bytes,
                   COALESCE(pg_total_relation_size(c.reltoastrelid), 0) AS toast_bytes,
                   COALESCE(s.n_live_tup, 0) AS live_rows,
                   COALESCE(s.n_dead_tup, 0) AS dead_rows,
                   s.last_autovacuum,
                   s.last_autoanalyze
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT :lim
            """),
        {"lim": limit},
    )
    rows = [
        TableSizeRow(
            schema_name=r[0],
            table_name=r[1],
            total_bytes=int(r[2] or 0),
            table_bytes=int(r[3] or 0),
            index_bytes=int(r[4] or 0),
            toast_bytes=int(r[5] or 0),
            live_rows=int(r[6] or 0),
            dead_rows=int(r[7] or 0),
            last_autovacuum=str(r[8]) if r[8] else None,
            last_autoanalyze=str(r[9]) if r[9] else None,
        )
        for r in res.all()
    ]
    return TableSizesResponse(rows=rows)


@router.get("/postgres/connections", response_model=ConnectionsResponse)
async def postgres_connections(db: DB, _: SuperAdmin) -> ConnectionsResponse:
    """Connection count grouped by state (active / idle / idle in transaction
    / idle in transaction (aborted) / disabled / fastpath function call).

    Lots of "idle in transaction" connections is the canonical signal for
    a stuck pool or a forgotten BEGIN somewhere in app code.
    """

    res = await db.execute(text("""
            SELECT COALESCE(state, 'unknown') AS state,
                   count(*) AS count
            FROM pg_stat_activity
            WHERE datname = current_database()
            GROUP BY 1
            ORDER BY 2 DESC
            """))
    rows = [ConnectionsRow(state=str(r[0]), count=int(r[1])) for r in res.all()]
    return ConnectionsResponse(rows=rows)


@router.get("/postgres/slow-queries", response_model=SlowQueriesResponse)
async def postgres_slow_queries(
    db: DB,
    _: SuperAdmin,
    limit: int = Query(20, ge=1, le=200),
) -> SlowQueriesResponse:
    """Top slow queries from ``pg_stat_statements`` if the extension is
    installed. Otherwise return ``available=false`` with a hint — we
    don't try to install it ourselves; that's an operator decision
    (it requires a postgresql.conf change + restart)."""

    has_ext = (
        await db.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements' LIMIT 1")
        )
    ).first()
    if not has_ext:
        return SlowQueriesResponse(
            available=False,
            hint=(
                "pg_stat_statements is not enabled. Add "
                "'shared_preload_libraries = pg_stat_statements' to postgresql.conf, "
                "restart Postgres, then 'CREATE EXTENSION pg_stat_statements;'."
            ),
        )

    # Newer PG (>=13) renames total_time → total_exec_time. Try the modern
    # form first; fall back if the column doesn't exist.
    try:
        res = await db.execute(
            text("""
                SELECT query,
                       calls,
                       total_exec_time AS total_time_ms,
                       mean_exec_time AS mean_time_ms,
                       rows
                FROM pg_stat_statements
                WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                ORDER BY total_exec_time DESC
                LIMIT :lim
                """),
            {"lim": limit},
        )
    except Exception:
        res = await db.execute(
            text("""
                SELECT query, calls, total_time, mean_time, rows
                FROM pg_stat_statements
                WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                ORDER BY total_time DESC
                LIMIT :lim
                """),
            {"lim": limit},
        )

    rows = [
        SlowQueryRow(
            query=r[0][:1000],  # truncate; raw queries can be huge
            calls=int(r[1] or 0),
            total_time_ms=float(r[2] or 0),
            mean_time_ms=float(r[3] or 0),
            rows=int(r[4] or 0),
        )
        for r in res.all()
    ]
    return SlowQueriesResponse(available=True, rows=rows)
