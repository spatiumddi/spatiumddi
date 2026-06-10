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
    # Issue #156 — syslog block present with its stable key set; off → empty.
    assert "syslog_settings" in body
    assert body["syslog_settings"]["enabled"] is False
    assert body["syslog_settings"]["config_hash"] == ""
    assert body["syslog_settings"]["ca_certs"] == {}


# ── Issue #156 — syslog bundle + delivery ─────────────────────────────


async def test_syslog_bundle_disabled_and_enabled_shapes() -> None:
    """syslog_bundle returns a STABLE dict shape disabled (empty body /
    hash) and a rendered body + hash when enabled."""
    from app.services.appliance.syslog import syslog_bundle

    off = PlatformSettings(id=1, syslog_enabled=False, syslog_targets=[])
    block = syslog_bundle(off)
    assert block == {
        "enabled": False,
        "config_hash": "",
        "rsyslog_conf": "",
        "ca_certs": {},
    }

    on = PlatformSettings(
        id=1,
        syslog_enabled=True,
        syslog_targets=[
            {"host": "collector.example", "port": 514, "protocol": "udp", "format": "rfc5424"}
        ],
        syslog_filter="*.*",
        syslog_buffer_disk=False,
    )
    block = syslog_bundle(on)
    assert block["enabled"] is True
    assert block["config_hash"]  # non-empty when enabled
    assert 'target="collector.example"' in block["rsyslog_conf"]
    # journald is forwarded — imjournal input block must be present.
    assert 'module(load="imjournal"' in block["rsyslog_conf"]
    assert block["ca_certs"] == {}


async def test_heartbeat_delivers_syslog_block(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await _settings(db_session)
    s.syslog_enabled = True
    s.syslog_targets = [
        {"host": "siem.example", "port": 6514, "protocol": "tcp", "format": "rfc5424"}
    ]
    s.syslog_filter = "*.*"
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={"appliance_id": str(row.id), "session_token": token},
    )
    assert r.status_code == 200, r.text
    block = r.json()["syslog_settings"]
    assert block["enabled"] is True
    assert block["config_hash"]
    assert 'target="siem.example"' in block["rsyslog_conf"]


async def test_syslog_targets_fold_into_dhcp_config_etag(db_session: AsyncSession) -> None:
    """Changing syslog_targets flips the DHCP /config ETag's syslog
    marker — the bundle helper is the same one the agent endpoint mixes
    into the fleet_marker, so the rendered config_hash must move."""
    from app.services.appliance.syslog import syslog_bundle

    s = PlatformSettings(
        id=1,
        syslog_enabled=True,
        syslog_targets=[{"host": "a.example", "port": 514, "protocol": "udp", "format": "rfc5424"}],
        syslog_filter="*.*",
    )
    hash_before = syslog_bundle(s)["config_hash"]
    s.syslog_targets = [{"host": "b.example", "port": 514, "protocol": "udp", "format": "rfc5424"}]
    hash_after = syslog_bundle(s)["config_hash"]
    assert hash_before != hash_after
