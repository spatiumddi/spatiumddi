"""Supervisor heartbeat long-poll (#358 Phase 1b).

The heartbeat gains an opt-in ``wait_seconds`` that asks the control
plane to hold the response open (Redis-woken) until a per-appliance
desired-state change or a bounded timeout, so operator commands
(upgrade / reboot / role / host-config) start in ~0 s instead of
waiting a full heartbeat interval.

These cases exercise the new branch deterministically — without a real
hold or a live Redis — via the two short-circuits:
* ``wait_seconds`` omitted (pre-#358 supervisor) → immediate return,
  ``long_poll=False`` (byte-for-byte the old behavior).
* ``wait_seconds`` > 0 but a concrete command is already pending →
  ``long_poll=True`` and an immediate return (a pending command is
  never delayed by the hold).
"""

from __future__ import annotations

import hashlib
import os
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.settings import PlatformSettings
from app.services.appliance.ca import generate_session_token


async def _enable(db: AsyncSession) -> None:
    s = await db.get(PlatformSettings, 1)
    if s is None:
        s = PlatformSettings(id=1)
        db.add(s)
    # Supervisor endpoints 404 unless the registration flag is on.
    s.supervisor_registration_enabled = True
    await db.flush()


async def _approved_supervisor(db: AsyncSession) -> tuple[Appliance, str]:
    token, token_hash = generate_session_token()
    der = os.urandom(32)
    row = Appliance(
        id=uuid.uuid4(),
        hostname="agent-longpoll",
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        session_token_hash=token_hash,
    )
    db.add(row)
    await db.flush()
    return row, token


async def test_heartbeat_long_poll_off_by_default(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # A supervisor that doesn't send wait_seconds (pre-#358) gets the old
    # immediate-return behavior with long_poll=False — keeps the
    # rolling-upgrade skew window safe.
    await _enable(db_session)
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={"appliance_id": str(row.id), "session_token": token},
    )
    assert r.status_code == 200, r.text
    assert r.json()["long_poll"] is False


async def test_heartbeat_long_poll_skips_hold_when_command_pending(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # wait_seconds>0 sets long_poll=True, but a concrete pending command
    # (a desired upgrade) short-circuits the hold so the command is never
    # delayed — the response returns immediately with the desired state.
    await _enable(db_session)
    row, token = await _approved_supervisor(db_session)
    row.desired_appliance_version = "2026.06.07-1"
    row.desired_slot_image_url = "https://example.invalid/slot.raw.xz"
    await db_session.commit()

    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={
            "appliance_id": str(row.id),
            "session_token": token,
            "wait_seconds": 5,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["long_poll"] is True
    assert body["desired_appliance_version"] == "2026.06.07-1"
