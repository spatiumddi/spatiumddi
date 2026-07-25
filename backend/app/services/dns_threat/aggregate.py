"""Roll DNS query-log lines up into per-client hourly windows (#699).

The raw ``dns_query_log_entry`` table is operator triage on a 24 h
window. This turns it into something a detection can reason about — one
summary row per client per hour — and, crucially, one that *outlives*
the raw lines. "This host has looked like an exfil tunnel every hour
for three days" has to stay answerable after the evidence rows are
pruned.

Bucketing is fixed hourly, aligned to the hour. The aggregator
recomputes the current bucket and the previous one on every run and
upserts, which makes it idempotent, self-healing against late-arriving
log lines (agents batch and can lag), and safe to run far more often
than the bucket width — the alternative, a trailing rolling window,
would produce overlapping rows that nothing could sensibly key on.

Rows are streamed ordered by client so grouping never holds more than
one client's queries in memory: a busy resolver can put six figures of
lines into a single hour, and this runs on the worker alongside
everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns_threat import DNSClientWindow
from app.models.logs import DNSQueryLogEntry
from app.services.dns_threat.scoring import (
    DEFAULT_BENIGN_PARENTS,
    extract_features,
    score_tunneling,
)

logger = structlog.get_logger(__name__)

# Windows outlive the raw log lines (24 h) by a wide margin — the whole
# point is retaining the summary after the evidence is gone — but not
# forever; this is behavioural metadata about clients.
WINDOW_RETENTION_DAYS = 30

# Scored windows below this are kept but are noise for every consumer;
# the Threat tab and the alert matcher both filter above it. Kept low so
# "nothing is scoring at all" stays distinguishable from "the feature
# isn't running".
INTERESTING_SCORE = 20.0


def floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


async def aggregate_window(
    db: AsyncSession,
    *,
    window_start: datetime,
    allowlist: frozenset[str] | set[str] = DEFAULT_BENIGN_PARENTS,
) -> int:
    """Compute + upsert every client's row for one hourly bucket.

    Returns the number of client rows written.
    """
    window_end = window_start + timedelta(hours=1)
    stmt = (
        select(
            DNSQueryLogEntry.client_ip,
            DNSQueryLogEntry.qname,
            DNSQueryLogEntry.qtype,
            DNSQueryLogEntry.server_id,
        ).where(
            DNSQueryLogEntry.ts >= window_start,
            DNSQueryLogEntry.ts < window_end,
            DNSQueryLogEntry.client_ip.is_not(None),
        )
        # Ordering by client is what lets the grouping below stream:
        # one client's rows arrive together, so only that client's
        # queries are ever held in memory.
        .order_by(DNSQueryLogEntry.client_ip)
    )

    written = 0
    current_ip: str | None = None
    batch: list[tuple[str | None, str | None, Any]] = []

    async def _flush() -> None:
        nonlocal written, batch
        if current_ip is None or not batch:
            return
        feats = extract_features(batch, allowlist=allowlist)
        verdict = score_tunneling(feats)
        await _upsert(
            db,
            client_ip=current_ip,
            window_start=window_start,
            window_end=window_end,
            feats=feats,
            score=verdict.score,
            signals=verdict.signals_json(),
        )
        written += 1
        batch = []

    result = await db.stream(stmt)
    async for client_ip, qname, qtype, server_id in result:
        ip = str(client_ip)
        if ip != current_ip:
            await _flush()
            current_ip = ip
        batch.append((qname, qtype, server_id))
    await _flush()

    await db.commit()
    return written


async def _upsert(
    db: AsyncSession,
    *,
    client_ip: str,
    window_start: datetime,
    window_end: datetime,
    feats: Any,
    score: float,
    signals: list[dict[str, Any]],
) -> None:
    """Insert or refresh one client's bucket.

    Upsert rather than insert because the same bucket is recomputed on
    subsequent runs as late log lines land; the unique constraint on
    (client_ip, window_start) is what the conflict target keys on.
    """
    values = {
        "client_ip": client_ip,
        "server_count": feats.server_count,
        "window_start": window_start,
        "window_end": window_end,
        "query_count": feats.query_count,
        "distinct_qnames": feats.distinct_qnames,
        "distinct_parents": feats.distinct_parents,
        "top_parent": feats.top_parent,
        "top_parent_subdomains": feats.top_parent_subdomains,
        "max_label_length": feats.max_label_length,
        "avg_label_length": feats.avg_label_length,
        "mean_label_entropy": feats.mean_label_entropy,
        "payload_qtype_count": feats.payload_qtype_count,
        "tunnel_score": score,
        "tunnel_signals": signals,
        "allowlisted": feats.allowlisted,
    }
    stmt = pg_insert(DNSClientWindow).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_dns_client_window_ip_bucket",
            set_={k: v for k, v in values.items() if k not in ("client_ip", "window_start")},
        )
    )


async def run_aggregation(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    allowlist: frozenset[str] | set[str] = DEFAULT_BENIGN_PARENTS,
) -> dict[str, int]:
    """Recompute the current and previous hourly buckets, then prune.

    Two buckets rather than one because agents batch their log shipping:
    a line generated at 10:59 can arrive at 11:02, after the 10:00
    bucket was first computed. Recomputing the previous hour picks it
    up. Anything later than that is lost to the rollup, which is an
    acceptable trade against unbounded recomputation.
    """
    now = now or datetime.now(UTC)
    current = floor_hour(now)
    previous = current - timedelta(hours=1)

    rows_current = await aggregate_window(db, window_start=current, allowlist=allowlist)
    rows_previous = await aggregate_window(db, window_start=previous, allowlist=allowlist)

    cutoff = now - timedelta(days=WINDOW_RETENTION_DAYS)
    pruned = await db.execute(delete(DNSClientWindow).where(DNSClientWindow.window_start < cutoff))
    await db.commit()

    return {
        "windows_current": rows_current,
        "windows_previous": rows_previous,
        "pruned": pruned.rowcount or 0,
    }
