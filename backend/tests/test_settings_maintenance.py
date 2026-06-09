"""Settings-router tests for maintenance mode (issue #57).

Covers:

* PUT /settings toggling on stamps ``maintenance_started_at`` and writes
  the audit row; toggling off clears the timestamp + writes the audit row.
* GET /settings + /health/platform expose the maintenance fields.
* An over-length ``maintenance_message`` (>500 chars) is rejected 422.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import maintenance_mode
from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User


async def _make_superadmin(db: AsyncSession, username: str = "mssuper") -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=True,
    )
    user.groups = []
    db.add(user)
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    maintenance_mode.invalidate_cache()


@pytest.mark.asyncio
async def test_enable_stamps_started_at_and_audits(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"maintenance_mode_enabled": True, "maintenance_message": "migrating"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["maintenance_mode_enabled"] is True
    assert body["maintenance_message"] == "migrating"
    assert body["maintenance_started_at"] is not None

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
    assert audit[0].resource_type == "platform_settings"
    assert audit[0].resource_id == "maintenance"
    assert audit[0].new_value == {"enabled": True, "message": "migrating"}


@pytest.mark.asyncio
async def test_disable_clears_started_at_and_audits(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    # Enable, then disable.
    await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"maintenance_mode_enabled": True},
    )
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"maintenance_mode_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["maintenance_mode_enabled"] is False
    assert body["maintenance_started_at"] is None

    disabled_audit = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "maintenance_mode.disabled")
            )
        )
        .scalars()
        .all()
    )
    assert len(disabled_audit) == 1


@pytest.mark.asyncio
async def test_no_flip_no_audit(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    # Writing the SAME (false) value must not produce an audit row or stamp.
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"maintenance_mode_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    audit = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action.in_(["maintenance_mode.enabled", "maintenance_mode.disabled"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert audit == []


@pytest.mark.asyncio
async def test_get_settings_exposes_fields(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get("/api/v1/settings", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "maintenance_mode_enabled" in body
    assert "maintenance_message" in body
    assert "maintenance_started_at" in body


@pytest.mark.asyncio
async def test_health_platform_exposes_fields(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"maintenance_mode_enabled": True, "maintenance_message": "hi"},
    )
    resp = await client.get("/health/platform", headers={"baseURL": "/"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["maintenance_mode"] is True
    assert body["maintenance_message"] == "hi"
    assert body["maintenance_started_at"] is not None


@pytest.mark.asyncio
async def test_overlong_message_422(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"maintenance_message": "x" * 501},
    )
    assert resp.status_code == 422, resp.text
