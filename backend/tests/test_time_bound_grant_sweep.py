"""Tests for the time-bound-grant expiry sweep (#65).

The sweep soft-revokes expired-but-not-yet-revoked grants, leaves live ones
alone, writes exactly one ``permission_change`` audit row per revoked grant
attributed to the ``system`` user, and is idempotent on re-run.

The task uses ``app.db.task_session()`` which builds its own engine against
``settings.database_url``; conftest points that at the per-worker test DB, so
we flush rows on ``db_session`` and ``commit`` before invoking ``_sweep``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.auth import Group
from app.models.time_bound_grant import TimeBoundGrant
from app.tasks.time_bound_grant_sweep import _sweep


async def _group(db: AsyncSession) -> Group:
    g = Group(name=f"g{uuid.uuid4().hex[:6]}", description="")
    db.add(g)
    await db.flush()
    return g


async def _grant(
    db: AsyncSession,
    group: Group,
    *,
    expires_at: datetime,
    revoked_at: datetime | None = None,
) -> TimeBoundGrant:
    grant = TimeBoundGrant(
        group_id=group.id,
        action="write",
        resource_type="subnet",
        resource_id=None,
        expires_at=expires_at,
        revoked_at=revoked_at,
        reason="sweep-test",
    )
    db.add(grant)
    await db.flush()
    return grant


async def test_sweep_revokes_expired_leaves_live(db_session: AsyncSession) -> None:
    g = await _group(db_session)
    now = datetime.now(UTC)
    expired = await _grant(db_session, g, expires_at=now - timedelta(minutes=5))
    live = await _grant(db_session, g, expires_at=now + timedelta(hours=1))
    already = await _grant(
        db_session, g, expires_at=now - timedelta(hours=2), revoked_at=now - timedelta(hours=1)
    )
    await db_session.commit()

    result = await _sweep()
    assert result["revoked"] == 1
    assert result["checked"] == 1

    await db_session.refresh(expired)
    await db_session.refresh(live)
    await db_session.refresh(already)
    assert expired.revoked_at is not None  # newly swept
    assert live.revoked_at is None  # still live
    assert already.revoked_at == (now - timedelta(hours=1))  # untouched

    # Exactly one permission_change audit row, attributed to system, for the
    # one grant we revoked this run.
    rows = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.resource_type == "time_bound_grant")
                .where(AuditLog.resource_id == str(expired.id))
                .where(AuditLog.action == "permission_change")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_display_name == "system"
    assert rows[0].user_id is None
    assert rows[0].new_value is not None
    assert rows[0].new_value.get("reason") == "time_bound_grant_expired"


async def test_sweep_idempotent(db_session: AsyncSession) -> None:
    g = await _group(db_session)
    now = datetime.now(UTC)
    expired = await _grant(db_session, g, expires_at=now - timedelta(minutes=5))
    await db_session.commit()

    first = await _sweep()
    assert first["revoked"] == 1

    second = await _sweep()
    assert second["revoked"] == 0  # nothing left to revoke
    assert second["checked"] == 0

    # Still exactly one audit row after the second (no-op) run.
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.resource_id == str(expired.id))
        .where(AuditLog.action == "permission_change")
    )
    assert count == 1
