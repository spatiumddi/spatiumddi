"""Settings-router tests for SSH config (issue #157).

Covers:

* PUT persists ssh_* fields (keys + toggles + port + source CIDRs).
* Lockout safety — disabling password auth with zero keys → 422.
* Malformed public key → 422.
* Privileged port (< 1024, not 22) → 422.
* A HOSTCONFIG_ALL wake fires on an ssh_* change.
* A dedicated audit row (resource_id='ssh') is written.
* Permission denial without write:settings → 403.
"""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.settings import PlatformSettings


def _make_ed25519_key(comment: str = "alice@host") -> str:
    name = b"ssh-ed25519"
    key = b"\x00" * 32
    blob = len(name).to_bytes(4, "big") + name + len(key).to_bytes(4, "big") + key
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii") + " " + comment


_ED = _make_ed25519_key()


async def _make_user(db: AsyncSession, *, username: str, superadmin: bool) -> tuple[User, str]:
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
    token = create_access_token(str(user.id))
    return user, token


@pytest.mark.asyncio
async def test_put_persists_ssh_fields(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="sshsuper1", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "ssh_authorized_keys": [{"name": "alice", "public_key": _ED, "comment": "laptop"}],
            "ssh_password_auth_enabled": False,
            "ssh_allow_root_login": True,
            "ssh_port": 2222,
            "ssh_allowed_source_networks": ["10.0.0.0/24"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ssh_password_auth_enabled"] is False
    assert body["ssh_allow_root_login"] is True
    assert body["ssh_port"] == 2222
    assert body["ssh_allowed_source_networks"] == ["10.0.0.0/24"]
    assert len(body["ssh_authorized_keys"]) == 1
    # Public keys are NOT secrets — returned verbatim.
    assert body["ssh_authorized_keys"][0]["public_key"] == _ED

    settings = await db_session.get(PlatformSettings, 1)
    assert settings is not None
    assert settings.ssh_port == 2222
    assert settings.ssh_authorized_keys[0]["name"] == "alice"


@pytest.mark.asyncio
async def test_disable_password_auth_no_keys_rejected(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, username="sshsuper2", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"ssh_password_auth_enabled": False, "ssh_authorized_keys": []},
    )
    assert resp.status_code == 422, resp.text
    assert "lock yourself out" in resp.text.lower() or "lock" in resp.text.lower()


@pytest.mark.asyncio
async def test_disable_password_auth_with_key_ok(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, username="sshsuper3", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "ssh_password_auth_enabled": False,
            "ssh_authorized_keys": [{"public_key": _ED}],
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_malformed_public_key_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="sshsuper4", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"ssh_authorized_keys": [{"public_key": "this is not a key"}]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_privileged_port_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="sshsuper5", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    # 80 is < 1024 and not 22 → rejected.
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"ssh_port": 80},
    )
    assert resp.status_code == 422, resp.text
    # 22 is the allowed exception.
    resp_ok = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"ssh_port": 22},
    )
    assert resp_ok.status_code == 200, resp_ok.text


@pytest.mark.asyncio
async def test_ssh_change_wakes_hostconfig(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await _make_user(db_session, username="sshsuper6", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    woke: list[str] = []

    async def _capture(channel: str) -> int:
        woke.append(channel)
        return 0

    monkeypatch.setattr("app.api.v1.settings.router.publish_wake", _capture)
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"ssh_allow_root_login": True},
    )
    assert resp.status_code == 200, resp.text
    from app.core.agent_wake import HOSTCONFIG_ALL

    assert HOSTCONFIG_ALL in woke


@pytest.mark.asyncio
async def test_audit_row_written(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="sshsuper7", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "ssh_authorized_keys": [{"name": "bob", "public_key": _ED, "comment": ""}],
            "ssh_allow_root_login": False,
        },
    )
    assert resp.status_code == 200, resp.text
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.resource_type == "platform_settings",
                    AuditLog.resource_id == "ssh",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].action == "update"
    assert rows[0].new_value["allow_root_login"] is False
    # Public keys are recorded in full (not secrets).
    assert rows[0].new_value["authorized_keys"][0]["public_key"] == _ED


@pytest.mark.asyncio
async def test_denied_without_write_settings(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="sshviewer", superadmin=False)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"ssh_allow_root_login": True},
    )
    assert resp.status_code == 403, resp.text
