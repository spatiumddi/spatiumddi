"""Integration tests for ``POST /api/v1/appliance/supervisor/register`` (#170 A2).

Covers:

* Feature flag — disabled returns 404 (the "shape an attacker probing
  for the path would see if it didn't exist").
* Happy path — claims the pairing code, creates an Appliance row in
  pending_approval, emits the two audit rows.
* Re-register from cache (same pubkey) — idempotent; doesn't burn a
  fresh pairing code; updates last_seen.
* Invalid pubkey — 422 BEFORE the pairing code is consumed.
* Pairing-code failure modes — all collapse to a single generic 403
  (unknown / expired / claimed / revoked).
* Pairing-code from a different agent kind (today's dns/dhcp/both)
  works for the supervisor too — the supervisor register flow is
  kind-agnostic.

The 500 ms failure friction is patched to 0 s in tests where it'd
just pad runtime.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.appliance import supervisor as supervisor_mod
from app.models.appliance import (
    APPLIANCE_STATE_PENDING_APPROVAL,
    Appliance,
    PairingCode,
)
from app.models.audit import AuditLog
from app.models.settings import PlatformSettings

# ── Helpers ────────────────────────────────────────────────────────


def _new_keypair() -> tuple[Ed25519PrivateKey, bytes, str, str]:
    """Return (priv, der, fingerprint_hex, b64-encoded-der)."""
    priv = Ed25519PrivateKey.generate()
    der = priv.public_key().public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )
    fp = hashlib.sha256(der).hexdigest()
    b64 = base64.b64encode(der).decode("ascii")
    return priv, der, fp, b64


async def _make_pairing_code(
    db: AsyncSession,
    *,
    code: str = "12345678",
    deployment_kind: str = "dns",
    expires_in_minutes: int = 15,
    used: bool = False,
    revoked: bool = False,
) -> PairingCode:
    row = PairingCode(
        id=uuid.uuid4(),
        code_hash=hashlib.sha256(code.encode("ascii")).hexdigest(),
        code_last_two=code[-2:],
        deployment_kind=deployment_kind,
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
        used_at=datetime.now(UTC) if used else None,
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    db.add(row)
    await db.flush()
    return row


async def _enable_supervisor_registration(db: AsyncSession) -> None:
    stmt = select(PlatformSettings).where(PlatformSettings.id == 1)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = PlatformSettings(id=1, supervisor_registration_enabled=True)
        db.add(row)
    else:
        row.supervisor_registration_enabled = True
    await db.flush()


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_failure_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor_mod, "_CONSUME_FAILURE_DELAY_S", 0.0)


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_404s_while_flag_disabled(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Default state — flag is FALSE on a fresh install. Even with a
    valid pairing code + pubkey, the endpoint must return 404, same
    shape as "endpoint does not exist"."""
    await _make_pairing_code(db_session, code="11111111")
    await db_session.commit()
    _, _, _, pubkey_b64 = _new_keypair()

    resp = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": "11111111",
            "hostname": "dns-east-1",
            "public_key_der_b64": pubkey_b64,
            "supervisor_version": "2026.05.14-1",
        },
    )
    assert resp.status_code == 404, resp.text

    # Pairing code untouched.
    row = (await db_session.execute(select(PairingCode))).scalars().one()
    assert row.used_at is None
    assert (await db_session.execute(select(Appliance))).scalars().first() is None


@pytest.mark.asyncio
async def test_register_happy_path(db_session: AsyncSession, client: AsyncClient) -> None:
    await _enable_supervisor_registration(db_session)
    code_row = await _make_pairing_code(db_session, code="22222222")
    await db_session.commit()

    _, _, fingerprint, pubkey_b64 = _new_keypair()

    resp = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": "22222222",
            "hostname": "dns-east-1",
            "public_key_der_b64": pubkey_b64,
            "supervisor_version": "2026.05.14-1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    appliance_id = uuid.UUID(body["appliance_id"])
    assert body["state"] == APPLIANCE_STATE_PENDING_APPROVAL
    assert body["public_key_fingerprint"] == fingerprint

    # Pairing code burned.
    await db_session.refresh(code_row)
    assert code_row.used_at is not None
    assert code_row.used_by_hostname == "dns-east-1"

    # Appliance row written.
    appliance = await db_session.get(Appliance, appliance_id)
    assert appliance is not None
    assert appliance.hostname == "dns-east-1"
    assert appliance.public_key_fingerprint == fingerprint
    assert appliance.supervisor_version == "2026.05.14-1"
    assert appliance.state == APPLIANCE_STATE_PENDING_APPROVAL
    assert appliance.paired_via_code_id == code_row.id

    # Both audit rows present (claim + registration_pending).
    audit_actions = {
        row.action
        for row in (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.resource_id.in_([str(code_row.id), str(appliance_id)])
                )
            )
        ).scalars()
    }
    assert "appliance.pairing_code_claimed" in audit_actions
    assert "appliance.registration_pending" in audit_actions


@pytest.mark.asyncio
async def test_register_is_idempotent_for_same_pubkey(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Crash-recovery shape — supervisor restarts before persisting
    its appliance_id, retries with the SAME pubkey. Endpoint must
    short-circuit to "you already exist" rather than refusing
    (because the code is now ``used_at != None``)."""
    await _enable_supervisor_registration(db_session)
    await _make_pairing_code(db_session, code="33333333")
    await db_session.commit()

    _, _, _, pubkey_b64 = _new_keypair()

    first = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": "33333333",
            "hostname": "dns-east-2",
            "public_key_der_b64": pubkey_b64,
            "supervisor_version": "2026.05.14-1",
        },
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["appliance_id"]

    # Same pubkey, fresh code (but doesn't matter — flow shouldn't
    # touch the code at all on the idempotent path).
    await _make_pairing_code(db_session, code="44444444")
    await db_session.commit()

    second = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": "44444444",
            "hostname": "dns-east-2-renamed",  # ignored on idempotent path
            "public_key_der_b64": pubkey_b64,
            "supervisor_version": "2026.05.14-2",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["appliance_id"] == first_id

    # The SECOND pairing code is still unused (idempotent path
    # short-circuits before touching it).
    stmt = select(PairingCode).where(
        PairingCode.code_hash == hashlib.sha256(b"44444444").hexdigest()
    )
    second_code = (await db_session.execute(stmt)).scalar_one()
    assert second_code.used_at is None

    # Version was updated on the existing row.
    appliance = await db_session.get(Appliance, uuid.UUID(first_id))
    assert appliance is not None
    assert appliance.supervisor_version == "2026.05.14-2"


@pytest.mark.asyncio
async def test_register_rejects_malformed_pubkey_without_burning_code(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _enable_supervisor_registration(db_session)
    code_row = await _make_pairing_code(db_session, code="55555555")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": "55555555",
            "hostname": "dns-east-3",
            "public_key_der_b64": "not-base64-at-all-$$$",
        },
    )
    assert resp.status_code == 422, resp.text

    await db_session.refresh(code_row)
    assert code_row.used_at is None


@pytest.mark.asyncio
async def test_register_rejects_non_ed25519_pubkey(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """An RSA pubkey is parseable DER but the wrong algorithm —
    must 422."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    await _enable_supervisor_registration(db_session)
    await _make_pairing_code(db_session, code="66666666")
    await db_session.commit()

    rsa_pub = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    rsa_der = rsa_pub.public_bytes(encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo)
    rsa_b64 = base64.b64encode(rsa_der).decode("ascii")

    resp = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": "66666666",
            "hostname": "dns-east-4",
            "public_key_der_b64": rsa_b64,
        },
    )
    assert resp.status_code == 422
    assert "Ed25519" in resp.text


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("unknown", id="unknown_code"),
        pytest.param("used", id="already_used"),
        pytest.param("revoked", id="revoked"),
        pytest.param("expired", id="expired"),
    ],
)
@pytest.mark.asyncio
async def test_register_collapses_pairing_failure_modes_to_403(
    db_session: AsyncSession, client: AsyncClient, state: str
) -> None:
    await _enable_supervisor_registration(db_session)

    if state == "unknown":
        code = "99999999"
        # no row inserted
    elif state == "used":
        await _make_pairing_code(db_session, code="77777771", used=True)
        code = "77777771"
    elif state == "revoked":
        await _make_pairing_code(db_session, code="77777772", revoked=True)
        code = "77777772"
    else:  # expired
        await _make_pairing_code(db_session, code="77777773", expires_in_minutes=-5)
        code = "77777773"
    await db_session.commit()

    _, _, _, pubkey_b64 = _new_keypair()
    resp = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": code,
            "hostname": f"dns-{state}",
            "public_key_der_b64": pubkey_b64,
        },
    )
    assert resp.status_code == 403, resp.text
    # All four reasons return the same generic message so timing-
    # invariant + shape-invariant.
    assert "invalid, expired, or already used" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_register_works_with_any_deployment_kind(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The supervisor flow doesn't care which deployment_kind the
    pairing code was minted for — A3 will drop the column, but in A2
    we just don't gate on it."""
    await _enable_supervisor_registration(db_session)
    await _make_pairing_code(db_session, code="88888888", deployment_kind="dhcp")
    await db_session.commit()
    _, _, _, pubkey_b64 = _new_keypair()

    resp = await client.post(
        "/api/v1/appliance/supervisor/register",
        json={
            "pairing_code": "88888888",
            "hostname": "dhcp-east-1",
            "public_key_der_b64": pubkey_b64,
        },
    )
    assert resp.status_code == 200, resp.text
