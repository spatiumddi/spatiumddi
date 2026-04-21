"""Celery entry point for the alerts evaluator.

Beat fires this every 60 s (see ``celery_app.py``). The task body is
idempotent — ``services.alerts.evaluate_all`` reads current state, opens
events for fresh matches, and resolves events whose condition cleared.
No internal gate: alerts always run when beat ticks. Individual rules
can be disabled via ``AlertRule.enabled``.
"""

from __future__ import annotations

import asyncio

import structlog

from app.celery_app import celery_app
from app.db import AsyncSessionLocal
from app.services import alerts as alert_service

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.alerts.evaluate_alerts")
def evaluate_alerts() -> dict[str, int]:
    return asyncio.run(_run())


async def _run() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        try:
            result = await alert_service.evaluate_all(session)
            if result["opened"] or result["resolved"]:
                logger.info("alerts_evaluated", **result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("alerts_evaluate_failed", error=str(exc))
            raise
