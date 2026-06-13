"""Settings-router tests for the supervisor-registration gate (#407).

``platform_settings.supervisor_registration_enabled`` gates appliance
(supervisor) pairing, but before #407 it was absent from both
``SettingsResponse`` and ``SettingsUpdate`` — so an operator on a generic
Kubernetes/Helm control plane (where the OS-appliance self-bootstrap never
fires) had no way to enable it short of hand-editing the database.

These tests pin the now-exposed round-trip:

* GET /settings surfaces the field (defaults False).
* PUT /settings can flip it on and back off; GET reflects the change.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import Group, Role, User


async def _make_superadmin(db: AsyncSession, username: str = "srsuper") -> tuple[User, str]:
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


async def _make_settings_editor(db: AsyncSession, username: str = "srsettings") -> tuple[User, str]:
    """A non-superadmin granted exactly ``write`` on ``settings`` — passes the
    generic write:settings gate but NOT the #407 superadmin gate on the
    registration flag."""
    role = Role(
        name=f"settings-editor-{uuid.uuid4().hex[:6]}",
        description="",
        permissions=[{"action": "write", "resource_type": "settings"}],
    )
    group = Group(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    group.roles = [role]
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=False,
    )
    user.groups = [group]
    db.add_all([role, group, user])
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


@pytest.mark.asyncio
async def test_get_settings_exposes_field_default_false(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session)
    await db_session.commit()
    resp = await client.get("/api/v1/settings", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "supervisor_registration_enabled" in body
    # Deliberate Wave-A security posture: off by default.
    assert body["supervisor_registration_enabled"] is False


@pytest.mark.asyncio
async def test_put_enables_then_disables_registration(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_superadmin(db_session, username="srtoggle")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    # Enable.
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"supervisor_registration_enabled": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["supervisor_registration_enabled"] is True
    # GET reflects the persisted change.
    resp = await client.get("/api/v1/settings", headers=headers)
    assert resp.json()["supervisor_registration_enabled"] is True

    # Disable again.
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"supervisor_registration_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["supervisor_registration_enabled"] is False
    resp = await client.get("/api/v1/settings", headers=headers)
    assert resp.json()["supervisor_registration_enabled"] is False


@pytest.mark.asyncio
async def test_non_superadmin_cannot_change_registration(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """#407 — enabling appliance registration is a security gate, so a
    delegated ``write:settings`` editor is 403'd (mirrors maintenance mode);
    the superadmin-only Fleet → Pairing toggle matches this."""
    _, token = await _make_settings_editor(db_session)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"supervisor_registration_enabled": True},
    )
    assert resp.status_code == 403, resp.text
    assert "superadmin" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_settings_editor_can_still_write_other_fields(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """#407 — the gate is field-scoped: a write:settings editor can still
    write non-registration settings."""
    _, token = await _make_settings_editor(db_session, username="srotherok")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"dns_auto_sync_enabled": True},
    )
    assert resp.status_code == 200, resp.text
