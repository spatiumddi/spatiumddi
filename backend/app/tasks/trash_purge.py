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
from typing import Any

import structlog
from sqlalchemy import and_, delete, or_, select

from app.celery_app import celery_app
from app.db import task_session
from app.models.audit import AuditLog
from app.models.dhcp import DHCPPool, DHCPScope, DHCPStaticAssignment
from app.models.dns import DNSRecord, DNSZone
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.models.settings import PlatformSettings
from app.services.dhcp.static_ipam import detach_ipam_for_static

logger = structlog.get_logger(__name__)

# Order matters — descendants first, ancestors last. ``DNSRecord`` cascades
# from ``DNSZone`` so deleting the zone first would orphan the records (the
# FK is CASCADE, so this is safe either way; we order it explicitly to keep
# the per-type counters honest — without ordering, the records would
# already be gone by the time we counted them and the row count would
# under-report). Same for ``DHCPStaticAssignment`` / ``DHCPPool`` under
# ``DHCPScope`` (#617).
_PURGE_MODELS_LEAF_FIRST: tuple[type, ...] = (
    DNSRecord,
    DHCPStaticAssignment,
    DHCPPool,
    DHCPScope,
    Subnet,
    IPBlock,
    DNSZone,
    IPSpace,
)


async def _release_ipam_mirrors(db: Any, cutoff: datetime) -> int:
    """Release the IPAM row behind every reservation this sweep is about to purge.

    The purge is a Core ``DELETE`` — it runs no per-row Python, so nothing would
    otherwise call the detach and the mirrored ``ip_address`` row would be left
    stranded at ``status="static_dhcp"`` pointing at a reservation Postgres had
    already removed: not allocated, not free, not reclaimable by any sweeper
    (#618). Run before the deletes, while the reservations are still readable.

    Selected by what the sweep will actually destroy, NOT by the reservation's
    own tombstone: the ``dhcp_scope`` DELETE below FK-cascades its reservations
    regardless of their ``deleted_at``, so keying only on the child's timestamp
    would miss any reservation whose stamp is absent or newer than its scope's
    (a pre-#617 row the migration backfill didn't reach, a clock skew, a future
    path that stamps parent and child separately) and strand its mirror.

    ``include_deleted`` because these rows are soft-deleted by definition — the
    global filter would hide the very rows we need to clean up.
    """
    res = await db.execute(
        select(DHCPStaticAssignment)
        .join(DHCPScope, DHCPStaticAssignment.scope_id == DHCPScope.id)
        .where(
            or_(
                and_(
                    DHCPStaticAssignment.deleted_at.is_not(None),
                    DHCPStaticAssignment.deleted_at < cutoff,
                ),
                and_(DHCPScope.deleted_at.is_not(None), DHCPScope.deleted_at < cutoff),
            )
        )
        .execution_options(include_deleted=True)
    )
    statics = list(res.scalars().all())
    for st in statics:
        await detach_ipam_for_static(db, st)
    if statics:
        await db.flush()
    return len(statics)


async def _sweep() -> dict[str, Any]:
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

        # Release IPAM mirrors before the Core DELETEs wipe the reservations.
        ipam_released = await _release_ipam_mirrors(db, cutoff)

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
                    new_value={
                        "counts": per_type,
                        "purge_days": purge_days,
                        "ipam_mirrors_released": ipam_released,
                    },
                    result="success",
                )
            )
        await db.commit()

        return {
            "removed": total_removed,
            "per_type": per_type,
            "ipam_mirrors_released": ipam_released,
            "purge_days": purge_days,
            "skipped": False,
        }


@celery_app.task(name="app.tasks.trash_purge.purge_expired_soft_deletes")
def purge_expired_soft_deletes() -> dict[str, Any]:
    result = asyncio.run(_sweep())
    logger.info("trash_purge.completed", **result)
    return result
