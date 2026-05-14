"""Integration tests for the appliance pairing-code surface (#169).

Covers the four endpoints end-to-end + the read MCP tool:

* Create — superadmin only; validates ``deployment_kind`` + optional
  group; refuses if bootstrap key is unset; returns the cleartext code
  exactly once.
* List — surfaces the redacted shape (``code_last_two`` only, never
  the full code or its hash); ``include_terminal=false`` filters to
  pending only.
* Revoke — idempotent on terminal rows.
* Consume — unauthenticated; correct code returns the bootstrap key;
  every failure mode (unknown / expired / claimed / revoked) returns
  a generic 403.

The 500 ms ``_CONSUME_FAILURE_DELAY_S`` brute-force friction is
deliberately monkeypatched to zero in tests where it'd otherwise pad
runtime — we're not testing the sleep, we're testing the response.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.appliance import pairing as pairing_mod
from app.config import settings
from app.core.security import create_access_token, hash_password
from app.models.appliance import PairingCode
from app.models.auth import User
from app.services.ai.tools.pairing import (
    FindPairingCodesArgs,
    find_pairing_codes,
)

# ── Fixtures ──────────────────────────────────────────────────────────


async def _make_user(
    db: AsyncSession,
    *,
    superadmin: bool = True,
    username: str = "pcadmin",
) -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


@pytest.fixture(autouse=True)
def _stub_bootstrap_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests run against an env where the keys aren't set. Patch
    them in so the create endpoint doesn't 409 with the "no
    *_AGENT_KEY configured" guard. Specific tests that exercise the
    unset case bypass this fixture by re-patching to ``""``.
    """
    monkeypatch.setattr(settings, "dns_agent_key", "dns-test-key-aaaaaaaa", raising=False)
    monkeypatch.setattr(settings, "dhcp_agent_key", "dhcp-test-key-bbbbbbbb", raising=False)


@pytest.fixture(autouse=True)
def _no_consume_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the brute-force-friction sleep on failed consume to 0s so
    failure-path tests don't add 500 ms per assertion."""
    monkeypatch.setattr(pairing_mod, "_CONSUME_FAILURE_DELAY_S", 0.0)


# ── Create ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_pairing_code_happy_path(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pccreate")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns", "note": "for dns-west-2"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Code is 8 decimal digits, returned exactly once in cleartext.
    assert len(body["code"]) == 8
    assert body["code"].isdigit()
    assert body["deployment_kind"] == "dns"
    assert body["note"] == "for dns-west-2"

    # Persisted with the hashed shape + last_two; cleartext never
    # stored.
    rows = (await db_session.execute(select(PairingCode))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.code_last_two == body["code"][-2:]
    assert len(row.code_hash) == 64
    assert row.code_hash != body["code"]
    assert row.used_at is None
    assert row.revoked_at is None


@pytest.mark.asyncio
async def test_create_pairing_code_non_superadmin_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=False, username="pcuser")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )
    # Either the permission gate (403) or the inner superadmin check
    # (403) — both are correct. Just assert not-allowed.
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_pairing_code_refuses_when_bootstrap_key_unset(
    db_session: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "dhcp_agent_key", "", raising=False)
    _, token = await _make_user(db_session, superadmin=True, username="pcnokey")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dhcp"},
    )
    assert resp.status_code == 409
    assert "DHCP_AGENT_KEY" in resp.text


@pytest.mark.asyncio
async def test_create_pairing_code_rejects_unknown_deployment_kind(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pcbadkind")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        # ``agent`` is reserved for #170 — not yet accepted.
        json={"deployment_kind": "agent"},
    )
    assert resp.status_code == 422


# ── List + Revoke ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_pairing_codes_redacts_cleartext(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pclist")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    # Mint a code.
    create_resp = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )
    full_code = create_resp.json()["code"]

    # List.
    list_resp = await client.get("/api/v1/appliance/pairing-codes", headers=headers)
    assert list_resp.status_code == 200
    rows = list_resp.json()["codes"]
    assert len(rows) == 1
    row = rows[0]
    # ``code_last_two`` is the only fragment of the code surfaced.
    assert row["code_last_two"] == full_code[-2:]
    assert "code" not in row
    assert "code_hash" not in row
    assert row["state"] == "pending"


@pytest.mark.asyncio
async def test_list_filters_terminal_when_include_terminal_false(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pclistflt")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    # One pending, one revoked.
    a = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )
    b = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dhcp"},
    )
    await client.delete(f"/api/v1/appliance/pairing-codes/{b.json()['id']}", headers=headers)

    full = await client.get("/api/v1/appliance/pairing-codes", headers=headers)
    pending_only = await client.get(
        "/api/v1/appliance/pairing-codes?include_terminal=false", headers=headers
    )
    assert len(full.json()["codes"]) == 2
    pending_rows = pending_only.json()["codes"]
    assert len(pending_rows) == 1
    assert pending_rows[0]["id"] == a.json()["id"]


@pytest.mark.asyncio
async def test_revoke_pairing_code_idempotent(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pcrevoke")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )
    code_id = resp.json()["id"]

    r1 = await client.delete(f"/api/v1/appliance/pairing-codes/{code_id}", headers=headers)
    r2 = await client.delete(f"/api/v1/appliance/pairing-codes/{code_id}", headers=headers)
    assert r1.status_code == 204
    # Re-revoking an already-revoked row is a no-op (still 204), not 409.
    assert r2.status_code == 204


# ── Consume (unauthenticated) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_happy_path_returns_bootstrap_key(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pcconsume")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )
    code = create.json()["code"]

    # Note: no Authorization header — pair is unauthenticated.
    resp = await client.post(
        "/api/v1/appliance/pair",
        json={"code": code, "hostname": "dns-west-2"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bootstrap_key"] == "dns-test-key-aaaaaaaa"
    assert body["deployment_kind"] == "dns"
    assert body["server_group_id"] is None

    # Row is now claimed.
    rows = (await db_session.execute(select(PairingCode))).scalars().all()
    assert len(rows) == 1
    assert rows[0].used_at is not None
    assert rows[0].used_by_hostname == "dns-west-2"


@pytest.mark.asyncio
async def test_consume_wrong_code_returns_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    resp = await client.post(
        "/api/v1/appliance/pair",
        json={"code": "00000000", "hostname": "rogue"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_consume_expired_code_returns_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """An expired row exists but should be refused."""
    _, token = await _make_user(db_session, superadmin=True, username="pcexp")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )
    code = create.json()["code"]
    code_id = create.json()["id"]

    # Backdate expiry to 1 min ago.
    row = await db_session.get(PairingCode, code_id)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/appliance/pair",
        json={"code": code, "hostname": "late"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_consume_already_used_code_returns_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pcused")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dhcp"},
    )
    code = create.json()["code"]

    r1 = await client.post("/api/v1/appliance/pair", json={"code": code, "hostname": "agent-a"})
    r2 = await client.post("/api/v1/appliance/pair", json={"code": code, "hostname": "agent-b"})
    assert r1.status_code == 200
    # Second consume of the same code is refused — the row's used_at
    # is now set.
    assert r2.status_code == 403


@pytest.mark.asyncio
async def test_consume_revoked_code_returns_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="pcrev2")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )
    code = create.json()["code"]
    await client.delete(f"/api/v1/appliance/pairing-codes/{create.json()['id']}", headers=headers)

    resp = await client.post(
        "/api/v1/appliance/pair", json={"code": code, "hostname": "after-revoke"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_consume_validates_code_shape(db_session: AsyncSession, client: AsyncClient) -> None:
    """Non-digit codes (e.g. someone pasting the long hex bootstrap
    key by mistake) are rejected at the schema layer."""
    resp = await client.post(
        "/api/v1/appliance/pair",
        json={"code": "abcdefgh", "hostname": "wrong-shape"},
    )
    assert resp.status_code == 422


# ── MCP tool ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_find_pairing_codes_refuses_non_superadmin(
    db_session: AsyncSession,
) -> None:
    user, _ = await _make_user(db_session, superadmin=False, username="pcmcpuser")
    out = await find_pairing_codes(db_session, user, FindPairingCodesArgs())
    assert isinstance(out, dict)
    assert "error" in out


@pytest.mark.asyncio
async def test_mcp_find_pairing_codes_redacts_secret_fields(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user, token = await _make_user(db_session, superadmin=True, username="pcmcpok")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/v1/appliance/pairing-codes",
        headers=headers,
        json={"deployment_kind": "dns"},
    )

    out = await find_pairing_codes(db_session, user, FindPairingCodesArgs())
    assert isinstance(out, dict)
    assert out["pending_count"] == 1
    row = out["codes"][0]
    # The cleartext code + the sha256 hash MUST NEVER appear in the
    # MCP response — only the last two digits.
    assert "code" not in row
    assert "code_hash" not in row
    assert len(row["code_last_two"]) == 2
