"""Operator Copilot maintenance-mode tool tests (issue #57).

Covers:

* ``maintenance_status`` returns the current state.
* ``set_maintenance_mode`` is registered as writes=True / default_enabled=False.
* ``set_maintenance_mode`` toggles state, stamps started_at, and audits.
* A non-superadmin gets an error (no mutation).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import maintenance_mode
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.services.ai.tools import REGISTRY
from app.services.ai.tools.maintenance import (
    MaintenanceStatusArgs,
    SetMaintenanceModeArgs,
    maintenance_status,
    set_maintenance_mode,
)


async def _make_user(db: AsyncSession, *, superadmin: bool, username: str) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=superadmin,
    )
    user.groups = []
    db.add(user)
    await db.flush()
    return user


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    maintenance_mode.invalidate_cache()


def test_tools_registered_with_expected_flags() -> None:
    status_tool = REGISTRY.get("maintenance_status")
    set_tool = REGISTRY.get("set_maintenance_mode")
    assert status_tool is not None
    assert set_tool is not None
    # Read tool: default-enabled, no writes.
    assert status_tool.writes is False
    assert status_tool.default_enabled is True
    # Write tool: writes + default-disabled (broad blast radius).
    assert set_tool.writes is True
    assert set_tool.default_enabled is False
    assert set_tool.module is None


@pytest.mark.asyncio
async def test_maintenance_status_reflects_state(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, superadmin=True, username="aistatus")
    out = await maintenance_status(db_session, user, MaintenanceStatusArgs())
    assert out["enabled"] is False

    # Flip on via the write tool, then re-read.
    await set_maintenance_mode(
        db_session, user, SetMaintenanceModeArgs(enabled=True, message="ai window")
    )
    maintenance_mode.invalidate_cache()
    out = await maintenance_status(db_session, user, MaintenanceStatusArgs())
    assert out["enabled"] is True
    assert out["message"] == "ai window"
    assert out["started_at"] is not None


@pytest.mark.asyncio
async def test_set_maintenance_mode_audits(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, superadmin=True, username="aiset")
    res = await set_maintenance_mode(
        db_session, user, SetMaintenanceModeArgs(enabled=True, message="x")
    )
    assert res["ok"] is True
    assert res["enabled"] is True
    assert res["changed"] is True
    assert res["started_at"] is not None

    audit = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "maintenance_mode.enabled")
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1
    assert audit[0].resource_id == "maintenance"


@pytest.mark.asyncio
async def test_set_maintenance_mode_non_superadmin_rejected(
    db_session: AsyncSession,
) -> None:
    user = await _make_user(db_session, superadmin=False, username="aiplain")
    res = await set_maintenance_mode(db_session, user, SetMaintenanceModeArgs(enabled=True))
    assert "error" in res
    # No audit row written.
    audit = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "maintenance_mode.enabled")
            )
        )
        .scalars()
        .all()
    )
    assert audit == []
