"""Beat-driven DNS threat rollup (issue #699).

Runs every 15 minutes, recomputing the current and previous hourly
buckets. Far more often than the bucket width on purpose: each run
upserts, so frequent runs mean the current hour's picture is close to
live rather than up to an hour stale, without any extra rows.

Double-gated, and both gates matter:

* the ``security.dns_threat`` feature module is default-OFF, because
  scoring reads query *content* and that is a privacy posture rather
  than a default;
* even enabled, there is nothing to roll up unless a DNS server group
  has query logging turned on.
"""

from __future__ import annotations

import asyncio

import structlog

from app.celery_app import celery_app
from app.db import task_session

logger = structlog.get_logger(__name__)


async def _run() -> dict[str, object]:
    from app.services.dns_threat.aggregate import run_aggregation  # noqa: PLC0415
    from app.services.feature_modules import is_module_enabled  # noqa: PLC0415

    async with task_session() as db:
        if not await is_module_enabled(db, "security.dns_threat"):
            return {"status": "module_disabled"}
        result = await run_aggregation(db)
        return {"status": "ok", **result}


@celery_app.task(
    name="app.tasks.dns_threat.aggregate_dns_client_windows",
    bind=True,
    autoretry_for=(OSError, ConnectionError),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
)
def aggregate_dns_client_windows(self) -> dict[str, object]:  # type: ignore[no-untyped-def]
    result = asyncio.run(_run())
    if result.get("status") == "ok" and (
        result.get("windows_current") or result.get("windows_previous")
    ):
        logger.info("dns_threat_rollup_tick", **result)
    return result
