"""Settings-router tests for syslog forwarding (issue #156).

Covers:

* PUT validation — bad port / protocol / format → 422; a TLS target with
  no CA → 422.
* CA PEM is Fernet-encrypted at rest + redacted to ``ca_cert_set`` on read.
* A dedicated audit row (resource_id='syslog') is written on a syslog change.
* Permission denial without write:settings → 403.
* Demo mode forbids the update → 403.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.settings import PlatformSettings


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


_VALID_TARGET = {
    "host": "collector.example",
    "port": 514,
    "protocol": "udp",
    "format": "rfc5424",
}


@pytest.mark.asyncio
async def test_bad_port_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="syssuper1", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"syslog_enabled": True, "syslog_targets": [{**_VALID_TARGET, "port": 0}]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_bad_protocol_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="syssuper2", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"syslog_targets": [{**_VALID_TARGET, "protocol": "carrier-pigeon"}]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_bad_format_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="syssuper3", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"syslog_targets": [{**_VALID_TARGET, "format": "csv"}]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_tls_without_ca_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="syssuper4", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    # No ca_cert_pem on a brand-new TLS target → 422 (no prior CA to keep).
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "syslog_enabled": True,
            "syslog_targets": [{**_VALID_TARGET, "protocol": "tls", "port": 6514}],
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_ca_encrypted_and_redacted(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="syssuper5", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    pem = "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n"
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "syslog_enabled": True,
            "syslog_targets": [
                {
                    "host": "siem.example",
                    "port": 6514,
                    "protocol": "tls",
                    "format": "rfc5424",
                    "ca_cert_pem": pem,
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    target = body["syslog_targets"][0]
    # Read shape carries ca_cert_set, NOT the PEM.
    assert target["ca_cert_set"] is True
    assert "ca_cert_pem" not in target

    # Stored value is Fernet ciphertext, not the plaintext PEM.
    settings = await db_session.get(PlatformSettings, 1)
    assert settings is not None
    stored = settings.syslog_targets[0]["ca_cert_pem"]
    assert stored is not None
    assert pem not in stored
    from app.core.crypto import decrypt_str

    assert decrypt_str(stored.encode("ascii")) == pem


@pytest.mark.asyncio
async def test_ca_leave_alone_on_edit(db_session: AsyncSession, client: AsyncClient) -> None:
    """Editing a TLS target's format without re-sending ca_cert_pem keeps
    the stored CA (merge keyed by host:port)."""
    _, token = await _make_user(db_session, username="syssuper6", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    await client.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "syslog_enabled": True,
            "syslog_targets": [
                {
                    "host": "siem.example",
                    "port": 6514,
                    "protocol": "tls",
                    "format": "rfc5424",
                    "ca_cert_pem": pem,
                }
            ],
        },
    )
    # Re-PUT the same target, new format, ca_cert_pem omitted → kept.
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={
            "syslog_targets": [
                {
                    "host": "siem.example",
                    "port": 6514,
                    "protocol": "tls",
                    "format": "json",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    settings = await db_session.get(PlatformSettings, 1)
    assert settings is not None
    stored = settings.syslog_targets[0]
    assert stored["format"] == "json"
    from app.core.crypto import decrypt_str

    assert decrypt_str(stored["ca_cert_pem"].encode("ascii")) == pem


@pytest.mark.asyncio
async def test_audit_row_written(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="syssuper7", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"syslog_enabled": True, "syslog_targets": [_VALID_TARGET], "syslog_filter": "*.*"},
    )
    assert resp.status_code == 200, resp.text
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.resource_type == "platform_settings",
                    AuditLog.resource_id == "syslog",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].action == "update"
    assert rows[0].new_value["enabled"] is True
    # The audit captures only the redacted target shape — no CA PEM.
    assert "ca_cert_pem" not in rows[0].new_value["targets"][0]


@pytest.mark.asyncio
async def test_denied_without_write_settings(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, username="sysviewer", superadmin=False)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"syslog_enabled": True},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_demo_mode_forbidden(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await _make_user(db_session, username="sysdemosuper", superadmin=True)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr("app.core.demo_mode.is_demo_mode", lambda: True)
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"syslog_enabled": True, "syslog_targets": [_VALID_TARGET]},
    )
    assert resp.status_code == 403, resp.text
