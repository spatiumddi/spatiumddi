"""Sweep change requests whose ``expires_at`` has passed — issue #62.

Beat fires every 60 s (always-on; the per-row ``expires_at`` is the only
knob — stamped at request time from the matched policy's ``ttl_hours``).
Idempotent: only matches rows that are still ``pending`` AND past
``expires_at``, so re-running on a quiet system picks up nothing new and a
row that an approver/requester already decided is never touched.

For each match the sweep flips ``pending`` → ``expired`` (terminal) and
writes one ``change_request.expired`` audit row attributed to the
synthetic ``system`` actor (NN #4 — every state change is audited). The
approve endpoint also lazily expires a row it finds stale on read, so a
request never executes past its TTL regardless of when this sweep runs;
the sweep is the durable bookkeeping layer that clears the queue.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from app.celery_app import celery_app
from app.db import task_session
from app.models.change_request import ChangeRequest
from app.services.approvals.service import mark_expired

logger = structlog.get_logger(__name__)


async def _sweep() -> dict[str, int]:
    async with task_session() as db:
        now = datetime.now(UTC)
        rows = list(
            (
                await db.execute(
                    select(ChangeRequest)
                    .where(ChangeRequest.state == "pending")
                    .where(ChangeRequest.expires_at < now)
                )
            )
            .scalars()
            .all()
        )
        for cr in rows:
            await mark_expired(db, cr)
        await db.commit()
        return {"checked": len(rows), "expired": len(rows)}


@celery_app.task(name="app.tasks.change_request_expiry.sweep_expired_change_requests")
def sweep_expired_change_requests() -> dict[str, int]:
    """Celery entry point. Idempotent — safe to retry."""
    result = asyncio.run(_sweep())
    logger.info("change_request_expiry_sweep_complete", **result)
    return result
