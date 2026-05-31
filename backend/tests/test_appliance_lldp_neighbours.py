"""LLDP neighbour ingest + find_lldp_neighbors MCP tool (issue #347).

The supervisor ships its local lldpd neighbours on the heartbeat; the handler
upserts them into ``appliance_lldp_neighbour`` and absence-deletes the rest.
"""

from __future__ import annotations

import hashlib
import os
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import User
from app.models.network import ApplianceLldpNeighbour
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


def _nbr(iface: str, chassis: str, port: str, **kw: object) -> dict:
    return {
        "local_iface": iface,
        "remote_chassis_id": chassis,
        "remote_port_id": port,
        **kw,
    }


async def _heartbeat(client: AsyncClient, row: Appliance, token: str, neighbours: list) -> None:
    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={
            "appliance_id": str(row.id),
            "session_token": token,
            "lldp_neighbours": neighbours,
        },
    )
    assert r.status_code == 200, r.text


async def _rows(db: AsyncSession, appliance_id: uuid.UUID) -> list[ApplianceLldpNeighbour]:
    res = await db.execute(
        select(ApplianceLldpNeighbour).where(ApplianceLldpNeighbour.appliance_id == appliance_id)
    )
    return list(res.scalars().all())


async def test_heartbeat_ingests_and_absence_deletes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()

    # First heartbeat — two neighbours.
    await _heartbeat(
        client,
        row,
        token,
        [
            _nbr(
                "eth0",
                "aa:bb:cc:00:00:01",
                "Gi0/1",
                remote_sys_name="sw1",
                remote_mgmt_ip="10.0.0.1",
            ),
            _nbr("eth1", "aa:bb:cc:00:00:02", "Gi0/2", remote_sys_name="sw2"),
        ],
    )
    db_session.expunge_all()
    rows = await _rows(db_session, row.id)
    assert len(rows) == 2
    by_iface = {r.local_iface: r for r in rows}
    assert by_iface["eth0"].remote_sys_name == "sw1"
    assert by_iface["eth0"].remote_mgmt_ip == "10.0.0.1"

    # Second heartbeat — eth0 updated, eth1 gone, eth2 new.
    await _heartbeat(
        client,
        row,
        token,
        [
            _nbr("eth0", "aa:bb:cc:00:00:01", "Gi0/1", remote_sys_name="sw1-renamed"),
            _nbr("eth2", "aa:bb:cc:00:00:09", "Gi0/9"),
        ],
    )
    db_session.expunge_all()
    rows2 = await _rows(db_session, row.id)
    ifaces = {r.local_iface for r in rows2}
    assert ifaces == {"eth0", "eth2"}  # eth1 absence-deleted
    eth0 = next(r for r in rows2 if r.local_iface == "eth0")
    assert eth0.remote_sys_name == "sw1-renamed"  # upserted


async def test_heartbeat_none_leaves_rows_alone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, token = await _approved_supervisor(db_session)
    await db_session.commit()
    await _heartbeat(client, row, token, [_nbr("eth0", "aa:bb:cc:00:00:01", "Gi0/1")])
    db_session.expunge_all()
    assert len(await _rows(db_session, row.id)) == 1

    # Heartbeat with lldp_neighbours omitted (None) → existing rows untouched.
    r = await client.post(
        "/api/v1/appliance/supervisor/heartbeat",
        json={"appliance_id": str(row.id), "session_token": token},
    )
    assert r.status_code == 200, r.text
    db_session.expunge_all()
    assert len(await _rows(db_session, row.id)) == 1


async def test_find_lldp_neighbors_tool(client: AsyncClient, db_session: AsyncSession) -> None:
    from app.services.ai.tools.lldp import FindLLDPNeighborsArgs, find_lldp_neighbors

    row, token = await _approved_supervisor(db_session)
    await db_session.commit()
    await _heartbeat(
        client,
        row,
        token,
        [_nbr("eth0", "aa:bb:cc:00:00:01", "Gi0/1", remote_sys_name="sw1")],
    )

    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="t",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db_session.add(user)
    await db_session.flush()

    out = await find_lldp_neighbors(db_session, user, FindLLDPNeighborsArgs())
    mine = [n for n in out if n["appliance_id"] == str(row.id)]
    assert len(mine) == 1
    assert mine[0]["remote_sys_name"] == "sw1"
    assert mine[0]["appliance_hostname"] == "agent-1"
    assert mine[0]["local_iface"] == "eth0"
