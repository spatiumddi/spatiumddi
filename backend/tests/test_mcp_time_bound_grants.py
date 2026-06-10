"""MCP-tool tests for the time-bound-grant copilot surface (#65).

* ``find_time_bound_grants`` is superadmin-gated (non-superadmin → error dict).
* ``propose_grant_temporary_access`` is ``default_enabled=False`` so it's
  absent from the default effective tool set.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import Group, User
from app.models.time_bound_grant import TimeBoundGrant
from app.services.ai.tools import auth_grants as ag
from app.services.ai.tools.base import REGISTRY, effective_tool_names


async def _user(db: AsyncSession, *, superadmin: bool) -> User:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password="x",
        is_superadmin=superadmin,
    )
    u.groups = []  # is_effective_superadmin walks .groups
    db.add(u)
    await db.flush()
    return u


def test_registration_metadata() -> None:
    find = REGISTRY.get("find_time_bound_grants")
    assert find is not None
    assert find.category == "admin"
    assert find.default_enabled is True  # read tool — discoverable by default
    assert find.writes is False

    propose = REGISTRY.get("propose_grant_temporary_access")
    assert propose is not None
    assert propose.category == "admin"
    # Minting permissions is high-blast-radius → opt-in.
    assert propose.default_enabled is False


def test_propose_absent_from_default_effective_set() -> None:
    eff = effective_tool_names(platform_enabled=None, provider_enabled=None)
    assert "propose_grant_temporary_access" not in eff
    # The read tool IS in the default set.
    assert "find_time_bound_grants" in eff


async def test_find_superadmin_gate_blocks_non_superadmin(db_session: AsyncSession) -> None:
    u = await _user(db_session, superadmin=False)
    res = await ag.find_time_bound_grants(db_session, u, ag.FindTimeBoundGrantsArgs())
    assert isinstance(res, list)
    assert len(res) == 1
    assert "error" in res[0]
    assert "superadmin" in res[0]["error"]


async def test_find_superadmin_returns_rows(db_session: AsyncSession) -> None:
    admin = await _user(db_session, superadmin=True)
    group = Group(name=f"g{uuid.uuid4().hex[:6]}", description="")
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        TimeBoundGrant(
            group_id=group.id,
            action="write",
            resource_type="subnet",
            resource_id=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            reason="live",
        )
    )
    await db_session.flush()

    res = await ag.find_time_bound_grants(db_session, admin, ag.FindTimeBoundGrantsArgs())
    assert isinstance(res, list)
    assert any(r.get("resource_type") == "subnet" and r.get("is_active") is True for r in res)
    # group_name resolved.
    assert any(r.get("group_name") == group.name for r in res)
