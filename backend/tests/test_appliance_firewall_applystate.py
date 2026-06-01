"""Heartbeat upserts FirewallApplyState (issue #285 Phase 2b).

The supervisor echoes the host runner's firewall apply-state sidecars
(applied hash / status / base marker); the handler upserts them into
firewall_apply_state with "only-when-not-None" semantics so a field
absent this tick is never clobbered, and an upsert (not get-or-create)
so overlapping heartbeats can't 500.
"""

from __future__ import annotations

import hashlib
import os
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.firewall import FirewallApplyState
from app.models.settings import PlatformSettings
from app.services.appliance.ca import generate_session_token


async def _approved_supervisor(db: AsyncSession) -> tuple[Appliance, str]:
    s = await db.get(PlatformSettings, 1)
    if s is None:
        s = PlatformSettings(id=1)
        db.add(s)
    s.supervisor_registration_enabled = True
    token, token_hash = generate_session_token()
    der = os.urandom(32)
    row = Appliance(
        id=uuid.uuid4(),
        hostname="cp-1",
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        session_token_hash=token_hash,
    )
    db.add(row)
    await db.flush()
    return row, token


async def _hb(client: AsyncClient, row: Appliance, token: str, **fields: object) -> None:
    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={"appliance_id": str(row.id), "session_token": token, **fields},
    )
    assert r.status_code == 200, r.text


async def _state(db: AsyncSession, appliance_id: uuid.UUID) -> FirewallApplyState | None:
    db.expunge_all()
    return (
        await db.execute(
            select(FirewallApplyState).where(FirewallApplyState.appliance_id == appliance_id)
        )
    ).scalar_one_or_none()


async def test_heartbeat_upserts_apply_state(client: AsyncClient, db_session: AsyncSession) -> None:
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    await _hb(
        client,
        row,
        token,
        firewall_applied_hash="a" * 64,
        firewall_applied_status="ok",
        firewall_base_marker="b" * 64,
    )
    st = await _state(db_session, row.id)
    assert st is not None
    assert st.applied_hash == "a" * 64
    assert st.applied_status == "ok"
    assert st.base_conf_marker == "b" * 64
    assert st.last_applied_at is not None


async def test_partial_update_leaves_others_intact(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    await _hb(
        client,
        row,
        token,
        firewall_applied_hash="a" * 64,
        firewall_applied_status="ok",
        firewall_base_marker="b" * 64,
    )
    # Second heartbeat: only the status changed (e.g. a later drift report);
    # hash + marker omitted → must NOT be clobbered (upsert set_ excludes them).
    await _hb(client, row, token, firewall_applied_status="error:apply")
    st = await _state(db_session, row.id)
    assert st is not None
    assert st.applied_status == "error:apply"
    assert st.applied_hash == "a" * 64  # untouched
    assert st.base_conf_marker == "b" * 64  # untouched


async def test_no_firewall_fields_creates_no_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # A heartbeat that reports no firewall state (legacy runner / pre-2b)
    # must not create an empty row.
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()
    await _hb(client, row, token)
    assert await _state(db_session, row.id) is None


async def test_repeated_heartbeat_no_500(client: AsyncClient, db_session: AsyncSession) -> None:
    # The upsert (on_conflict_do_update) must tolerate the row already
    # existing — a second heartbeat with the same fields can't 500.
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()
    await _hb(client, row, token, firewall_applied_status="ok")
    await _hb(client, row, token, firewall_applied_status="ok")
    st = await _state(db_session, row.id)
    assert st is not None and st.applied_status == "ok"
