"""#623 — a no-primary zone's stale DNS records are still deletable via Sync DNS.

When a scope / reservation delete removes the IPAM address, its auto-generated A
record becomes stale ("ip-deleted"). If the zone has no primary DNS server, the
wire delete can't be pushed — but the record then exists ONLY in our DB, so there
is no wire state to diverge from and it must still be removable. Previously the
reconcile refused ("no primary configured — wire delete skipped", "0 deleted"),
leaving the stale record un-cleanable in the subnet's DNS view.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPBlock, IPSpace, Subnet


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"ds-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="DNS Sync Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


@pytest.mark.asyncio
async def test_no_primary_stale_record_is_deletable(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)

    # A DNS group with NO server → resolve_primary_server returns None, so the
    # batched wire delete drops (op_row is None) — the "no primary" case.
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db_session.add(grp)
    await db_session.flush()
    zone = DNSZone(
        group_id=grp.id,
        name="np.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.np.example.",
        admin_email="admin.np.example.",
    )
    db_session.add(zone)
    await db_session.flush()
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(space)
    await db_session.flush()
    block = IPBlock(space_id=space.id, network="10.70.0.0/16", name="blk")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.70.0.0/24",
        name="sn",
        dns_zone_id=str(zone.id),
        dns_inherit_settings=False,
    )
    db_session.add(subnet)
    await db_session.flush()

    # A stale auto-generated A record: its IPAM address is already gone
    # (ip_address_id NULL) — exactly the shape a scope/static delete leaves in a
    # zone with no primary DNS server.
    rec = DNSRecord(
        zone_id=zone.id,
        name="ghost",
        fqdn="ghost.np.example.",
        record_type="A",
        value="10.70.0.9",
        auto_generated=True,
        ip_address_id=None,
    )
    db_session.add(rec)
    await db_session.flush()
    rec_id = rec.id
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/dns-sync/commit",
        headers=headers,
        json={"delete_stale_record_ids": [str(rec_id)]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Removed from the DB even though there was no primary to push the wire
    # delete to — no "wire delete skipped" error, one record deleted.
    assert body["deleted"] == 1, body
    assert body["errors"] == [], body

    db_session.expire_all()
    assert (
        await db_session.execute(select(DNSRecord).where(DNSRecord.id == rec_id))
    ).scalar_one_or_none() is None
