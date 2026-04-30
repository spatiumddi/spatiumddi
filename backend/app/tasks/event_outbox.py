"""Celery beat task that drains the typed-event outbox.

Runs every 10 s by default — enough for "near real-time" downstream
automation while keeping the worker pressure low. The task itself is
idempotent: ``process_due_outbox`` uses ``SELECT … FOR UPDATE SKIP
LOCKED`` so multiple worker replicas (or beat-late ticks) cooperate
without double-delivery.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import celery_app
from app.config import settings as _app_settings
from app.services.event_delivery import process_due_outbox

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.event_outbox.process_event_outbox")
def process_event_outbox() -> dict[str, object]:
    """Tick the event outbox worker.

    Uses a fresh ``NullPool`` engine per tick. Celery's prefork worker
    reuses processes across tasks, and SQLAlchemy's pooled engines
    bind asyncpg connections to the loop that first checked them out —
    re-entering with ``asyncio.run`` (a new loop) surfaces as
    ``RuntimeError: Future attached to a different loop`` or
    ``InterfaceError: another operation is in progress``. ``NullPool``
    has no cross-task connection state to leak.
    """

    async def _run() -> dict[str, int]:
        engine = create_async_engine(_app_settings.database_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await process_due_outbox(session)
        finally:
            await engine.dispose()

    try:
        return dict(asyncio.run(_run()))
    except Exception as exc:  # noqa: BLE001
        logger.exception("event_outbox_tick_failed", error=str(exc))
        return {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0, "error": str(exc)}
