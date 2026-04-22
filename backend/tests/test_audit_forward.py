"""Tests for the multi-target / multi-format audit forwarding.

Covers the interesting behaviors: per-format output shape, target
filtering (min_severity, resource_types), and multi-target fan-out.
Transport-level code (UDP / TCP / TLS sockets) is exercised by the
lowest-cost path — we assert the formatter output and mock the
network send, not the real wire.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.services import audit_forward as svc

# ── Payload helper ─────────────────────────────────────────────────


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "evt-1",
        "timestamp": "2026-04-22T12:00:00+00:00",
        "action": "create",
        "resource_type": "dns_zone",
        "resource_id": "z-1",
        "resource_display": "example.com.",
        "result": "success",
        "user_id": "u-1",
        "user_display_name": "alice",
        "auth_source": "local",
        "changed_fields": ["name"],
        "old_value": None,
        "new_value": {"name": "example.com."},
    }
    base.update(overrides)
    return base


# ── Formatter unit tests ───────────────────────────────────────────


def test_rfc5424_json_has_pri_and_body():
    out = svc.render_for_target("rfc5424_json", facility=16, payload=_payload())
    assert out.startswith("<134>1 ")  # 16<<3 | 6 = 134 (facility=local0, sev=info)
    assert '"action":"create"' in out
    assert '"resource_type":"dns_zone"' in out


def test_rfc5424_cef_header_and_extensions():
    out = svc.render_for_target("rfc5424_cef", facility=16, payload=_payload())
    assert "CEF:0|SpatiumDDI|SpatiumDDI|1.0|dns_zone:create|example.com.|3" in out
    assert "act=create" in out
    assert "suser=alice" in out


def test_cef_denied_severity_maps_to_9():
    out = svc.render_for_target("rfc5424_cef", facility=16, payload=_payload(result="denied"))
    # "|9" after the name field
    assert "|example.com.|9|" in out


def test_leef_header_and_delimiter():
    out = svc.render_for_target("rfc5424_leef", facility=16, payload=_payload())
    assert "LEEF:2.0|SpatiumDDI|SpatiumDDI|1.0|dns_zone:create|^" in out
    # Fields separated by caret per header spec
    assert "^act=create" in out or "act=create^" in out


def test_rfc3164_prefix():
    out = svc.render_for_target("rfc3164", facility=16, payload=_payload())
    # <PRI>Mmm dd HH:MM:SS host tag: {json}
    assert out.startswith("<134>Apr 22 ")
    assert out.endswith('"new_value":{"name":"example.com."}}')


def test_json_lines_no_syslog_wrapper():
    out = svc.render_for_target("json_lines", facility=16, payload=_payload())
    assert out.startswith("{")
    assert out.endswith("}")
    assert "<134>" not in out


def test_unknown_format_falls_back_to_rfc5424_json():
    out = svc.render_for_target("bogus", facility=16, payload=_payload())
    assert out.startswith("<134>1 ")


# ── CEF / LEEF escaping ────────────────────────────────────────────


def test_cef_extension_escapes_equals_and_backslash():
    out = svc.render_for_target(
        "rfc5424_cef",
        facility=16,
        payload=_payload(user_display_name="bob=b\\ad"),
    )
    assert "suser=bob\\=b\\\\ad" in out


def test_cef_header_escapes_pipe():
    out = svc.render_for_target(
        "rfc5424_cef",
        facility=16,
        payload=_payload(resource_display="weird|name"),
    )
    # The pipe in the name field is escaped; extensions don't touch it.
    assert "|weird\\|name|" in out


# ── Filter tests ───────────────────────────────────────────────────


def test_min_severity_filter_blocks_lower():
    target = {
        "kind": "syslog",
        "min_severity": "error",
        "resource_types": None,
    }
    assert svc._target_accepts(target, _payload(result="success")) is False
    assert svc._target_accepts(target, _payload(result="error")) is True
    assert svc._target_accepts(target, _payload(result="denied")) is True


def test_resource_types_allowlist():
    target = {
        "kind": "syslog",
        "min_severity": None,
        "resource_types": ["dns_zone", "subnet"],
    }
    assert svc._target_accepts(target, _payload(resource_type="dns_zone")) is True
    assert svc._target_accepts(target, _payload(resource_type="dhcp_scope")) is False


def test_no_filter_accepts_everything():
    target = {"kind": "syslog", "min_severity": None, "resource_types": None}
    assert svc._target_accepts(target, _payload(result="success")) is True
    assert svc._target_accepts(target, _payload(result="denied")) is True


# ── Deliver fan-out ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_syslog_calls_send_syslog() -> None:
    target = {
        "name": "t",
        "kind": "syslog",
        "format": "rfc5424_json",
        "host": "10.0.0.1",
        "port": 514,
        "protocol": "udp",
        "facility": 16,
        "ca_cert_pem": None,
        "min_severity": None,
        "resource_types": None,
    }
    with patch.object(svc, "_send_syslog", new=AsyncMock()) as mock_send:
        await svc._deliver_to_target(target, _payload())
    assert mock_send.await_count == 1
    args = mock_send.await_args.args
    assert args[0] == "10.0.0.1"
    assert args[1] == 514
    assert args[2] == "udp"
    assert args[3].startswith("<134>1 ")


@pytest.mark.asyncio
async def test_deliver_webhook_calls_send_webhook() -> None:
    target = {
        "name": "wh",
        "kind": "webhook",
        "url": "https://example.com/ingest",
        "auth_header": "Bearer abc",
        "min_severity": None,
        "resource_types": None,
    }
    with patch.object(svc, "_send_webhook", new=AsyncMock()) as mock_wh:
        await svc._deliver_to_target(target, _payload())
    mock_wh.assert_awaited_once_with("https://example.com/ingest", "Bearer abc", _payload())


@pytest.mark.asyncio
async def test_filter_short_circuits_before_send() -> None:
    target = {
        "name": "quiet",
        "kind": "syslog",
        "format": "rfc5424_json",
        "host": "10.0.0.1",
        "port": 514,
        "protocol": "udp",
        "facility": 16,
        "ca_cert_pem": None,
        "min_severity": "denied",
        "resource_types": None,
    }
    with patch.object(svc, "_send_syslog", new=AsyncMock()) as mock_send:
        await svc._deliver_to_target(target, _payload(result="success"))
    mock_send.assert_not_awaited()


# ── CRUD API round-trip ────────────────────────────────────────────


async def _make_user(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


@pytest.mark.asyncio
async def test_crud_roundtrip(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    h = {"Authorization": f"Bearer {token}"}

    # Start empty
    r = await client.get("/api/v1/settings/audit-forward-targets", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == []

    # Create syslog
    body = {
        "name": "SIEM prod",
        "enabled": True,
        "kind": "syslog",
        "format": "rfc5424_cef",
        "host": "siem.example.com",
        "port": 6514,
        "protocol": "tls",
        "facility": 16,
    }
    r = await client.post("/api/v1/settings/audit-forward-targets", headers=h, json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    target_id = created["id"]
    assert created["format"] == "rfc5424_cef"
    assert created["protocol"] == "tls"

    # List
    r = await client.get("/api/v1/settings/audit-forward-targets", headers=h)
    assert len(r.json()) == 1

    # Update
    r = await client.put(
        f"/api/v1/settings/audit-forward-targets/{target_id}",
        headers=h,
        json={**body, "port": 1514, "min_severity": "error"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["port"] == 1514
    assert r.json()["min_severity"] == "error"

    # Delete
    r = await client.delete(f"/api/v1/settings/audit-forward-targets/{target_id}", headers=h)
    assert r.status_code == 204
    r = await client.get("/api/v1/settings/audit-forward-targets", headers=h)
    assert r.json() == []


@pytest.mark.asyncio
async def test_invalid_format_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    r = await client.post(
        "/api/v1/settings/audit-forward-targets",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "bad",
            "enabled": True,
            "kind": "syslog",
            "format": "not-a-format",
            "host": "x",
        },
    )
    assert r.status_code == 422


# Fallback / preempt tests for ``_load_targets`` are deliberately
# omitted: the service opens its own ``AsyncSessionLocal`` which binds
# to the app DB, not the fixture-managed test DB. The two-session
# split gets in the way of a clean assertion here. The behavior is
# exercised in real upgrades and covered at the CRUD-roundtrip level
# (a row written through the API shows up in a subsequent GET).
