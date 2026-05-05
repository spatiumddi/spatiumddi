"""Celery entry point for the conformity evaluator.

Beat fires this every minute (see ``celery_app.py``). The task body is
idempotent — ``services.conformity.evaluate_due_policies`` only runs
policies whose ``last_evaluated_at + eval_interval_hours`` is in
the past, so the practical cadence is the per-policy
``eval_interval_hours`` (default 24 h).
"""

from __future__ import annotations

import asyncio

import structlog

from app.celery_app import celery_app
from app.db import task_session
from app.services.conformity import evaluate_due_policies

logger = structlog.get_logger(__name__)


@celery_app.task(name="app.tasks.conformity.evaluate_conformity")
def evaluate_conformity() -> dict[str, int]:
    return asyncio.run(_run())


async def _run() -> dict[str, int]:
    async with task_session() as session:
        try:
            result = await evaluate_due_policies(session)
            if result["policies_evaluated"]:
                logger.info("conformity_evaluated", **result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("conformity_evaluate_failed", error=str(exc))
            raise
