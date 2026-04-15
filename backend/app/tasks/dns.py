"""DNS background tasks (blocklist feed refresh, zone push, etc.)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery_app
from app.config import settings
from app.models.dns import DNSBlockList, DNSBlockListEntry
from app.services.dns_blocklist import parse_feed

logger = structlog.get_logger(__name__)


async def _refresh_blocklist_feed_async(list_id: str) -> dict[str, int | str]:
    """Core async logic for refresh_blocklist_feed, reusable from tests."""
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            bl = (
                await db.execute(select(DNSBlockList).where(DNSBlockList.id == list_id))
            ).scalar_one_or_none()
            if bl is None:
                return {"status": "not_found", "added": 0, "removed": 0}

            if not bl.feed_url:
                bl.last_sync_status = "error"
                bl.last_sync_error = "No feed_url configured"
                bl.last_synced_at = datetime.now(UTC)
                await db.commit()
                return {"status": "error", "added": 0, "removed": 0}

            try:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(bl.feed_url)
                    resp.raise_for_status()
                    text = resp.text
            except Exception as e:  # noqa: BLE001
                bl.last_sync_status = "error"
                bl.last_sync_error = f"Fetch failed: {e}"
                bl.last_synced_at = datetime.now(UTC)
                await db.commit()
                logger.exception(
                    "blocklist_feed_fetch_failed", list_id=list_id, error=str(e)
                )
                return {"status": "error", "added": 0, "removed": 0}

            domains = set(parse_feed(text, bl.feed_format))

            # Load current feed-sourced entries
            existing_result = await db.execute(
                select(DNSBlockListEntry).where(
                    DNSBlockListEntry.list_id == bl.id,
                    DNSBlockListEntry.source == "feed",
                )
            )
            existing = {e.domain: e for e in existing_result.scalars().all()}

            # Compute diff
            to_add = domains - set(existing.keys())
            to_remove = set(existing.keys()) - domains

            for d in to_add:
                db.add(
                    DNSBlockListEntry(
                        list_id=bl.id,
                        domain=d,
                        entry_type="block",
                        source="feed",
                    )
                )

            for d in to_remove:
                await db.delete(existing[d])

            # Recompute count
            count_result = await db.execute(
                select(DNSBlockListEntry).where(DNSBlockListEntry.list_id == bl.id)
            )
            bl.entry_count = len(count_result.scalars().all()) + len(to_add) - len(to_remove)
            bl.last_synced_at = datetime.now(UTC)
            bl.last_sync_status = "success"
            bl.last_sync_error = None
            await db.commit()

            logger.info(
                "blocklist_feed_refreshed",
                list_id=list_id,
                added=len(to_add),
                removed=len(to_remove),
            )
            return {
                "status": "success",
                "added": len(to_add),
                "removed": len(to_remove),
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.dns.refresh_blocklist_feed", bind=True, max_retries=3)
def refresh_blocklist_feed(self: object, list_id: str) -> dict[str, int | str]:  # type: ignore[type-arg]
    """Fetch feed_url, parse as hosts/domain/adblock list, sync entries with source=feed.

    Idempotent — safe to retry. Only manages entries with source="feed"; manual
    entries added by users are never touched.
    """
    logger.info("refresh_blocklist_feed_started", list_id=list_id)
    return asyncio.run(_refresh_blocklist_feed_async(list_id))


# ── Agent stale-sweep ──────────────────────────────────────────────────────────

AGENT_STALE_AFTER_SECONDS = 90  # 3× heartbeat interval per DNS_AGENT.md §4


async def _dns_agent_stale_sweep_async() -> dict[str, int]:
    """Mark agents stale when no heartbeat seen for AGENT_STALE_AFTER_SECONDS.

    Idempotent — only flips status for servers whose status is currently
    'active' but whose last_seen_at is beyond the threshold.
    """
    from datetime import timedelta

    from sqlalchemy import update

    from app.models.dns import DNSServer

    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            cutoff = datetime.now(UTC) - timedelta(seconds=AGENT_STALE_AFTER_SECONDS)
            res = await db.execute(
                update(DNSServer)
                .where(
                    DNSServer.status == "active",
                    DNSServer.last_seen_at.isnot(None),
                    DNSServer.last_seen_at < cutoff,
                )
                .values(status="unreachable")
                .returning(DNSServer.id)
            )
            changed = len(res.all())
            await db.commit()
            if changed:
                logger.info("dns_agent_stale_sweep", marked_unreachable=changed)
            return {"marked_unreachable": changed}
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.dns.agent_stale_sweep")
def agent_stale_sweep() -> dict[str, int]:
    """Celery beat task — runs every 60s, flips stale agents to 'unreachable'."""
    return asyncio.run(_dns_agent_stale_sweep_async())
