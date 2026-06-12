"""Heartbeat persists per-plane host-config apply health (#387).

The supervisor's bounded-retry fire-guard reports, per hash-keyed
host-config plane (ntp / snmp / …), whether the desired config is
applied or stuck. The heartbeat carries ``host_config_health`` and the
handler overwrites the ``appliance.host_config_health`` column verbatim
(empty dict clears stale entries; field omitted → untouched), and
``ApplianceRow`` exposes it so the Fleet drilldown can surface a stuck
apply instead of the pre-#387 silent re-fire loop.
"""

from __future__ import annotations

import hashlib
import os
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import User
from app.models.settings import PlatformSettings
from app.services.appliance.ca import generate_session_token

_STUCK = {"ntp": {"state": "failing", "attempts": 4, "at": "2026-06-12T12:00:00+00:00"}}


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


async def _reload(db: AsyncSession, appliance_id: uuid.UUID) -> Appliance:
    db.expunge_all()
    return await db.get(Appliance, appliance_id)  # type: ignore[return-value]


async def test_heartbeat_persists_stuck_plane(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    await _hb(client, row, token, host_config_health=_STUCK)
    refreshed = await _reload(db_session, row.id)
    assert refreshed.host_config_health == _STUCK


async def test_empty_dict_clears_stale_entries(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    await _hb(client, row, token, host_config_health=_STUCK)
    # Plane converged → supervisor ships {} → must clear (overwrite verbatim).
    await _hb(client, row, token, host_config_health={})
    refreshed = await _reload(db_session, row.id)
    assert refreshed.host_config_health == {}


async def test_omitted_field_left_untouched(client: AsyncClient, db_session: AsyncSession) -> None:
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    await _hb(client, row, token, host_config_health=_STUCK)
    # A pre-#387 supervisor omits the field entirely → None → don't clobber.
    await _hb(client, row, token)
    refreshed = await _reload(db_session, row.id)
    assert refreshed.host_config_health == _STUCK


async def test_appliance_row_exposes_host_config_health(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _approved_supervisor(db_session)
    admin = User(
        username=f"admin-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Admin",
        hashed_password=hash_password("pw-387"),
        is_superadmin=True,
    )
    db_session.add(admin)
    await db_session.commit()

    await _hb(client, row, token, host_config_health=_STUCK)
    resp = await client.get(
        "/api/v1/appliance/appliances",
        headers={"Authorization": f"Bearer {create_access_token(str(admin.id))}"},
    )
    assert resp.status_code == 200, resp.text
    found = next(a for a in resp.json()["appliances"] if a["id"] == str(row.id))
    assert found["host_config_health"] == _STUCK
