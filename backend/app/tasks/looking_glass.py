"""BGP Looking Glass collector housekeeping tasks (issue #566).

``collector_stale_sweep`` mirrors ``app.tasks.dns.agent_stale_sweep``:
a beat task that flips a ``LookingGlassCollector`` to ``unreachable`` once
its heartbeat has been silent past the staleness window, so a dead collector
doesn't sit frozen at ``active`` forever in the Sessions / Fleet UI.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery_app
from app.config import settings
from app.models.bgp_looking_glass import LookingGlassCollector

logger = structlog.get_logger(__name__)

# Generous multiple of the collector heartbeat interval so a single missed
# beat doesn't flap the row to unreachable.
COLLECTOR_STALE_AFTER_SECONDS = 180


async def _collector_stale_sweep_async() -> dict[str, int]:
    """Flip collectors to ``unreachable`` when no heartbeat seen past the window.

    Idempotent — only touches rows currently ``active`` whose ``last_seen_at``
    is beyond the cutoff.
    """
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            cutoff = datetime.now(UTC) - timedelta(seconds=COLLECTOR_STALE_AFTER_SECONDS)
            res = await db.execute(
                update(LookingGlassCollector)
                .where(
                    LookingGlassCollector.status == "active",
                    LookingGlassCollector.last_seen_at.isnot(None),
                    LookingGlassCollector.last_seen_at < cutoff,
                )
                .values(status="unreachable")
                .returning(LookingGlassCollector.id)
            )
            changed = len(res.all())
            await db.commit()
            if changed:
                logger.info("lg_collector_stale_sweep", marked_unreachable=changed)
            return {"marked_unreachable": changed}
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.looking_glass.collector_stale_sweep")
def collector_stale_sweep() -> dict[str, int]:
    """Celery beat task — flips stale Looking Glass collectors to 'unreachable'."""
    return asyncio.run(_collector_stale_sweep_async())
