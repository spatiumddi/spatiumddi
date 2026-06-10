"""Time-bound grant helpers — issue #65.

Shared by the auth dependency (which stashes the caller's live grants on
``User._active_time_bound_grants``), the permission helper (which unions
those grants over the static role grants), and the expiry sweep.

The two predicates here reuse the exact matching semantics from
``app.core.permissions`` so a temporary grant evaluates identically to a
static role permission with the same triple.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    _action_matches,
    _resource_id_matches,
    _resource_type_matches,
)
from app.models.time_bound_grant import TimeBoundGrant


async def load_active_grants_for_groups(
    db: AsyncSession, group_ids: Sequence[uuid.UUID]
) -> list[TimeBoundGrant]:
    """Return every live time-bound grant for the given groups.

    "Live" = ``revoked_at IS NULL`` AND ``expires_at > now()``. The
    ``expires_at`` filter is belt-and-suspenders: it makes expiry effective
    immediately at request time even before the 60 s sweep flips
    ``revoked_at``. Covered by ``ix_time_bound_grant_live``.

    Returns an empty list when ``group_ids`` is empty so callers can stash it
    unconditionally.
    """
    if not group_ids:
        return []
    now = datetime.now(UTC)
    stmt = (
        select(TimeBoundGrant)
        .where(TimeBoundGrant.group_id.in_(group_ids))
        .where(TimeBoundGrant.revoked_at.is_(None))
        .where(TimeBoundGrant.expires_at > now)
    )
    return list((await db.execute(stmt)).scalars().all())


def grant_matches(
    grant: TimeBoundGrant,
    action: str,
    resource_type: str,
    resource_id: str | None,
) -> bool:
    """Whether ``grant`` covers the requested ``(action, resource_type,
    resource_id)`` triple — same predicates a static role permission uses."""
    if not _action_matches(grant.action, action):
        return False
    if not _resource_type_matches(grant.resource_type, resource_type):
        return False
    if not _resource_id_matches(grant.resource_id, resource_id):
        return False
    return True
