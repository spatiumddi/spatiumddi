"""Supervisor heartbeat delivers host-config blocks (issue #346).

The keystone for the appliance host-config plane: the supervisor heartbeat
response must carry the rendered snmp / chrony / lldp blocks so the
supervisor can fire the host-side reload triggers. Before #346 the response
omitted them entirely (SNMP #153 / NTP #154 / LLDP #343 rendered config that
never reached the supervisor).
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


async def _settings(db: AsyncSession) -> PlatformSettings:
    s = await db.get(PlatformSettings, 1)
    if s is None:
        s = PlatformSettings(id=1)
        db.add(s)
    # The supervisor endpoints 404 unless the feature flag is on.
    s.supervisor_registration_enabled = True
    s.lldp_enabled = True
    s.lldp_tx_interval = 30
    s.lldp_tx_hold = 4
    s.lldp_protocols = ["cdp"]
    s.snmp_enabled = False
    await db.flush()
    return s


async def _approved_supervisor(db: AsyncSession) -> tuple[Appliance, str]:
    token, token_hash = generate_session_token()
    der = os.urandom(32)
    row = Appliance(
        id=uuid.uuid4(),
        hostname="agent-1",
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        deployment_kind="appliance",
        session_token_hash=token_hash,
    )
    db.add(row)
    await db.flush()
    return row, token


async def test_heartbeat_delivers_host_config_blocks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _settings(db_session)
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={"appliance_id": str(row.id), "session_token": token},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # All three host-config blocks are present with their stable key set.
    assert "snmp_settings" in body and "ntp_settings" in body and "lldp_settings" in body
    lldp = body["lldp_settings"]
    assert lldp["enabled"] is True
    assert lldp["config_hash"]  # non-empty when enabled
    assert "configure lldp tx-interval 30" in lldp["lldpd_conf"]
    assert lldp["daemon_args"] == "-c"  # cdp reception
    # SNMP is off → disabled-shape block (still present, empty hash).
    assert body["snmp_settings"]["enabled"] is False
    assert body["snmp_settings"]["config_hash"] == ""
    # NTP block carries its stable keys too.
    assert "config_hash" in body["ntp_settings"]
