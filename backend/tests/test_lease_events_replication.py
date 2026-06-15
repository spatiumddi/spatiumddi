"""#428 — Kea lease-event ingestion → IPAM mirror (regression guard).

The agent posts the server's ``LeaseEventBatch`` shape; the endpoint must
upsert a DHCPLease + mirror it into IPAM as an ``auto_from_lease`` row for
the containing subnet. Two guards:

* the agent-shaped payload (``{"leases":[{"ip_address",…}]}``) is accepted
  and actually creates rows (the blocker was a silent 200-no-op), and
* the OLD/wrong envelope (``{"events":[…]}``) now 422s loudly instead of
  validating to an empty batch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dhcp.agents import _auth_agent
from app.main import app
from app.models.dhcp import DHCPLease, DHCPServer
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


async def _seed(db: AsyncSession) -> tuple[DHCPServer, Subnet]:
    space = IPSpace(name=f"le-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.70.0.0/16", name="le-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.70.1.0/24", name="le-sn")
    db.add(subnet)
    server = DHCPServer(
        name=f"le-kea-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        port=67,
        status="active",
    )
    db.add(server)
    await db.flush()
    return server, subnet


def _agent_payload(ip: str = "10.70.1.50", mac: str = "aa:bb:cc:dd:ee:ff") -> dict:
    end = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    # Exactly what the fixed agent ships (server LeaseEvent field names).
    return {
        "leases": [
            {
                "ip_address": ip,
                "mac_address": mac,
                "hostname": "host1",
                "state": "active",
                "starts_at": datetime.now(UTC).isoformat(),
                "ends_at": end,
                "expires_at": end,
            }
        ]
    }


@pytest.mark.asyncio
async def test_agent_shaped_lease_event_mirrors_to_ipam(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    server, subnet = await _seed(db_session)
    await db_session.commit()

    app.dependency_overrides[_auth_agent] = lambda: (server, {})
    try:
        resp = await client.post("/api/v1/dhcp/agents/lease-events", json=_agent_payload())
    finally:
        app.dependency_overrides.pop(_auth_agent, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["upserted"] == 1

    # DHCPLease row created with a non-NULL expires_at (so the time-based
    # sweep can reap it) ...
    lease = (
        await db_session.execute(select(DHCPLease).where(DHCPLease.ip_address == "10.70.1.50"))
    ).scalar_one()
    assert lease.mac_address == "aa:bb:cc:dd:ee:ff"
    assert lease.expires_at is not None

    # ... and mirrored into IPAM as an auto_from_lease row in the subnet.
    addr = (
        await db_session.execute(select(IPAddress).where(IPAddress.address == "10.70.1.50"))
    ).scalar_one()
    assert addr.subnet_id == subnet.id
    assert addr.auto_from_lease is True
    assert addr.mac_address == "aa:bb:cc:dd:ee:ff"


@pytest.mark.asyncio
async def test_old_events_envelope_is_rejected_loudly(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    server, _ = await _seed(db_session)
    await db_session.commit()

    app.dependency_overrides[_auth_agent] = lambda: (server, {})
    try:
        # The pre-#428 shape: wrong envelope key + wrong field names. Must
        # 422 (extra=forbid) instead of silently validating to an empty
        # batch and returning 200.
        resp = await client.post(
            "/api/v1/dhcp/agents/lease-events",
            json={"events": [{"ip": "10.70.1.50", "mac": "aa:bb:cc:dd:ee:ff"}]},
        )
    finally:
        app.dependency_overrides.pop(_auth_agent, None)

    assert resp.status_code == 422, resp.text
    # And no lease/IPAM row leaked through.
    leaked = (
        await db_session.execute(select(DHCPLease).where(DHCPLease.ip_address == "10.70.1.50"))
    ).scalar_one_or_none()
    assert leaked is None
