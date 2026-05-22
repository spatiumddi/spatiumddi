"""Tests for ``POST /api/v1/appliance/self-register-bootstrap`` (#272 Phase 1).

The endpoint mints a one-shot pairing code for the local supervisor on
full-stack / frontend-core appliances, where the installer wizard
didn't capture a pairing code (the control plane is local). Gates:

* Variant must be ``full-stack`` / ``frontend-core``.
* The api's host bind-mounted ``role-config:ROLE`` must match.
* No LIVE ``Appliance`` row may exist (``last_seen_at IS NOT NULL``);
  orphan rows from a botched earlier attempt get cleared.
* Module gate (``supervisor_registration_enabled``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.appliance import supervisor as supervisor_mod
from app.models.appliance import Appliance, PairingCode
from app.models.audit import AuditLog
from app.models.settings import PlatformSettings

# ── Helpers ────────────────────────────────────────────────────────


async def _enable_supervisor_registration(db: AsyncSession) -> None:
    stmt = select(PlatformSettings).where(PlatformSettings.id == 1)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = PlatformSettings(id=1, supervisor_registration_enabled=True)
        db.add(row)
    else:
        row.supervisor_registration_enabled = True
    await db.flush()


@pytest.fixture(autouse=True)
def _no_failure_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor_mod, "_CONSUME_FAILURE_DELAY_S", 0.0)


@pytest.fixture
def stub_host_role(monkeypatch: pytest.MonkeyPatch):
    """Yields a setter so each test picks the host role independently.

    Production reads from /etc/spatiumddi-host/role-config; tests
    patch ``_read_host_role`` to return the value the test claims
    the host is configured as. Returning ``None`` simulates a
    non-appliance deploy (no bind mount).
    """

    def _set(role: str | None) -> None:
        monkeypatch.setattr(supervisor_mod, "_read_host_role", lambda: role)

    return _set


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_works_without_module_gate_enabled(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    # The self-bootstrap endpoint deliberately does NOT honour the
    # ``supervisor_registration_enabled`` gate — the local supervisor
    # MUST be able to register or the appliance is unusable. The
    # gate applies to the public /supervisor/register endpoint only.
    # No PlatformSettings row → gate is off → endpoint still works.
    stub_host_role("full-stack")
    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "full-stack"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_happy_path_mints_code(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    await _enable_supervisor_registration(db_session)
    await db_session.commit()
    stub_host_role("full-stack")

    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "full-stack"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["code"]) == 8
    assert body["code"].isdigit()
    assert body["control_plane_url"].startswith("http://")
    assert "spatium-control-spatiumddi-api" in body["control_plane_url"]
    assert body["expires_in_seconds"] == 600

    # A pairing_code row landed with a 10-min expiry.
    rows = (await db_session.execute(select(PairingCode))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.persistent is False
    assert row.code_last_two == body["code"][-2:]
    assert row.expires_at is not None
    assert row.expires_at - datetime.now(UTC) > timedelta(minutes=9)

    # Audit row written.
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "appliance.self_bootstrap_minted")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_frontend_core_variant_accepted(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    await _enable_supervisor_registration(db_session)
    await db_session.commit()
    stub_host_role("frontend-core")

    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "frontend-core"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_variant_mismatch_refused(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    # Caller claims full-stack but host actually frontend-core.
    # Pretending to be a different variant doesn't let you mint codes
    # for it.
    await _enable_supervisor_registration(db_session)
    await db_session.commit()
    stub_host_role("frontend-core")

    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "full-stack"},
    )
    assert resp.status_code == 403
    # No pairing code created.
    rows = (await db_session.execute(select(PairingCode))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_no_host_role_config_refused(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    # Non-appliance deploy — role-config not mounted. Caller could
    # be anywhere on the cluster; we refuse.
    await _enable_supervisor_registration(db_session)
    await db_session.commit()
    stub_host_role(None)

    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "full-stack"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_application_variant_refused(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    # Application variant uses the operator-typed pairing-code flow.
    # The endpoint refuses to short-circuit that.
    await _enable_supervisor_registration(db_session)
    await db_session.commit()
    stub_host_role("application")  # contrived — application normally has CONTROL_PLANE_URL filled

    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "application"},  # pydantic Literal blocks this
    )
    # Pydantic rejects the unknown literal at validation time → 422.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_single_shot_after_live_appliance(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    # Pre-existing LIVE Appliance row (last_seen_at IS NOT NULL)
    # simulates a control plane that's already registered a
    # heartbeating supervisor — the endpoint refuses 409.
    await _enable_supervisor_registration(db_session)
    db_session.add(
        Appliance(
            hostname="test",
            public_key_der=b"\x00" * 32,
            public_key_fingerprint="a" * 64,
            state="approved",
            last_seen_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    stub_host_role("full-stack")

    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "full-stack"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_orphan_appliance_cleared_on_self_bootstrap(
    db_session: AsyncSession, client: AsyncClient, stub_host_role
) -> None:
    # Phase 1 audit follow-up — a pre-existing row with
    # ``last_seen_at IS NULL`` is an orphan from a botched earlier
    # self-bootstrap. The endpoint clears it + mints a fresh code
    # so the supervisor can recover without operator intervention.
    await _enable_supervisor_registration(db_session)
    orphan = Appliance(
        hostname="test1",
        public_key_der=b"\x00" * 32,
        public_key_fingerprint="a" * 64,
        state="approved",
        last_seen_at=None,
    )
    db_session.add(orphan)
    await db_session.commit()
    orphan_id = orphan.id
    stub_host_role("full-stack")

    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "full-stack"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["code"]) == 8
    assert body["control_plane_url"].startswith("http")

    # Orphan row deleted; audit row records the clearance.
    remaining = (await db_session.execute(select(Appliance))).scalars().all()
    assert orphan_id not in {a.id for a in remaining}
    audit_actions = {
        a.action
        for a in (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.resource_id == str(orphan_id))
                )
            )
            .scalars()
            .all()
        )
    }
    assert "appliance.self_bootstrap_orphan_cleared" in audit_actions
