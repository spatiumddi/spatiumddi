"""Nightly retention sweep for agent-shipped log entries.

Deletes ``dns_query_log_entry`` and ``dhcp_log_entry`` rows older
than ``DEFAULT_RETENTION_HOURS`` (24 h). Query logs are operator-
triage tooling, not analytics — keeping a short rolling window
caps the table size on busy resolvers (10k+ qps means ~36M rows in
an hour, so even at 24 h we're already managing a substantial
table — we deliberately avoid a longer default).

Symmetric to ``prune_metrics``: tick once a day, delete everything
past the cutoff. Runs under the default queue.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.db import AsyncSessionLocal
from app.models.logs import DHCPLogEntry, DNSQueryLogEntry

logger = structlog.get_logger(__name__)

# Operator-triage window. Anyone wanting longer history should
# stand up Loki / a SIEM and ship there in addition to (or instead
# of) the agent push.
DEFAULT_RETENTION_HOURS = 24


async def _sweep_with_session(db: AsyncSession) -> dict[str, int]:
    """Run the prune against an explicit session.

    Split out so tests can hand in their pytest-managed session
    against the test database — the celery wrapper opens its own
    against the prod ``DATABASE_URL``.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=DEFAULT_RETENTION_HOURS)
    dns_del = await db.execute(delete(DNSQueryLogEntry).where(DNSQueryLogEntry.ts < cutoff))
    dhcp_del = await db.execute(delete(DHCPLogEntry).where(DHCPLogEntry.ts < cutoff))
    await db.commit()
    return {
        "dns_query_log_removed": dns_del.rowcount or 0,
        "dhcp_log_removed": dhcp_del.rowcount or 0,
        "retention_hours": DEFAULT_RETENTION_HOURS,
    }


async def _sweep() -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        return await _sweep_with_session(db)


@celery_app.task(name="app.tasks.prune_logs.prune_log_entries")
def prune_log_entries() -> dict[str, int]:
    result = asyncio.run(_sweep())
    logger.info("log_entries_pruned", **result)
    return result
