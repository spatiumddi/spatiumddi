"""Daily purge sweep — hard-delete soft-deleted rows older than the
retention window.

Gated on ``PlatformSettings.soft_delete_purge_days`` (default 30). Setting
the value to 0 disables the purge entirely (rows accumulate forever; manual
permanent-delete via the trash UI is still available).

Counters are emitted per resource type and logged at the end of the run.
A single audit-log row records the summary so operators can see in one
place "the trash sweep ran, here's what it removed".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, select

from app.celery_app import celery_app
from app.db import task_session
from app.models.audit import AuditLog
from app.models.dhcp import DHCPScope
from app.models.dns import DNSRecord, DNSZone
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.models.settings import PlatformSettings

logger = structlog.get_logger(__name__)

# Order matters — descendants first, ancestors last. ``DNSRecord`` cascades
# from ``DNSZone`` so deleting the zone first would orphan the records (the
# FK is CASCADE, so this is safe either way; we order it explicitly to keep
# the per-type counters honest — without ordering, the records would
# already be gone by the time we counted them and the row count would
# under-report).
_PURGE_MODELS_LEAF_FIRST: tuple[type, ...] = (
    DNSRecord,
    DHCPScope,
    Subnet,
    IPBlock,
    DNSZone,
    IPSpace,
)


async def _sweep() -> dict[str, int]:
    async with task_session() as db:
        ps_res = await db.execute(select(PlatformSettings).limit(1))
        ps = ps_res.scalar_one_or_none()
        purge_days = 30
        if ps is not None:
            configured = getattr(ps, "soft_delete_purge_days", None)
            if isinstance(configured, int):
                purge_days = configured

        if purge_days <= 0:
            logger.info("trash_purge.disabled", purge_days=purge_days)
            return {"removed": 0, "purge_days": purge_days, "skipped": True}

        cutoff = datetime.now(UTC) - timedelta(days=purge_days)
        per_type: dict[str, int] = {}
        total_removed = 0

        for model in _PURGE_MODELS_LEAF_FIRST:
            stmt = (
                delete(model)
                .where(model.deleted_at.is_not(None))
                .where(model.deleted_at < cutoff)
                .execution_options(include_deleted=True)
            )
            res = await db.execute(stmt)
            removed = int(res.rowcount or 0)
            per_type[model.__tablename__] = removed
            total_removed += removed

        if total_removed:
            db.add(
                AuditLog(
                    user_id=None,
                    user_display_name="system",
                    auth_source="system",
                    action="purge",
                    resource_type="trash",
                    resource_id="sweep",
                    resource_display=f"{total_removed} rows purged",
                    new_value={"counts": per_type, "purge_days": purge_days},
                    result="success",
                )
            )
        await db.commit()

        return {
            "removed": total_removed,
            "per_type": per_type,
            "purge_days": purge_days,
            "skipped": False,
        }


@celery_app.task(name="app.tasks.trash_purge.purge_expired_soft_deletes")
def purge_expired_soft_deletes() -> dict[str, int]:
    result = asyncio.run(_sweep())
    logger.info("trash_purge.completed", **result)
    return result
