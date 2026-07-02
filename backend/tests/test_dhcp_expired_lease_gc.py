"""#478 — expired agent-lease GC + manual delete-lease + static-detach status.

Agent-based (Kea) servers have no absence-delete reconciler, and the agent's
expired-event branch drops only the IPAM mirror — so ``expired`` DHCPLease rows
piled up in the view forever. The time-based sweep now hard-deletes them past a
24h grace, a DELETE endpoint lets an operator drop one now, and detaching a
static frees its IPAM row to ``available`` (not ``allocated``) so a new dynamic
lease can reclaim it instead of being shadowed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import (
    DHCPLease,
    DHCPLeaseHistory,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


async def _server(db: AsyncSession) -> tuple[DHCPServerGroup, DHCPServer]:
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    return grp, srv


async def _subnet(db: AsyncSession, grp: DHCPServerGroup) -> tuple[Subnet, DHCPScope]:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/16", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="s")
    db.add(subnet)
    await db.flush()
    scope = DHCPScope(group_id=grp.id, subnet_id=subnet.id, is_active=True)
    db.add(scope)
    await db.flush()
    return subnet, scope


async def _superadmin_token(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


@pytest.mark.asyncio
async def test_sweep_hard_deletes_stale_expired_leases(db_session: AsyncSession) -> None:
    import app.tasks.dhcp_lease_cleanup as cleanup

    _, srv = await _server(db_session)
    now = datetime.now(UTC)
    stale = DHCPLease(
        server_id=srv.id,
        ip_address="10.0.0.10",
        mac_address="aa:bb:cc:dd:ee:10",
        state="expired",
        expires_at=now - timedelta(hours=25),  # past the 24h GC grace
    )
    recent = DHCPLease(
        server_id=srv.id,
        ip_address="10.0.0.11",
        mac_address="aa:bb:cc:dd:ee:11",
        state="expired",
        expires_at=now - timedelta(hours=1),  # still within grace
    )
    db_session.add_all([stale, recent])
    await db_session.commit()

    cleaned, deleted = await cleanup._sweep()
    assert deleted == 1

    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.id == stale.id))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.id == recent.id))
    ).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_delete_lease_endpoint_removes_lease_and_mirror(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _superadmin_token(db_session)
    grp, srv = await _server(db_session)
    subnet, scope = await _subnet(db_session, grp)
    lease = DHCPLease(
        server_id=srv.id,
        scope_id=scope.id,
        ip_address="10.0.0.30",
        mac_address="aa:bb:cc:dd:ee:30",
        state="expired",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    mirror = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.30",
        status="dhcp",
        mac_address="aa:bb:cc:dd:ee:30",
        auto_from_lease=True,
    )
    db_session.add_all([lease, mirror])
    await db_session.commit()

    r = await client.delete(
        f"/api/v1/dhcp/servers/{srv.id}/leases/{lease.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204, r.text

    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.id == lease.id))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror.id))
    ).scalar_one_or_none() is None
    hist = (
        (
            await db_session.execute(
                select(DHCPLeaseHistory).where(DHCPLeaseHistory.server_id == srv.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(h.lease_state == "removed" for h in hist)


@pytest.mark.asyncio
async def test_detach_static_frees_ipam_row_to_available(db_session: AsyncSession) -> None:
    from app.api.v1.dhcp.statics import _detach_ipam_for_static

    grp, _srv = await _server(db_session)
    subnet, scope = await _subnet(db_session, grp)
    st = DHCPStaticAssignment(
        scope_id=scope.id, ip_address="10.0.0.20", mac_address="aa:bb:cc:dd:ee:20"
    )
    db_session.add(st)
    await db_session.flush()
    row = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.20",
        status="static_dhcp",
        static_assignment_id=str(st.id),
        auto_from_lease=False,
    )
    db_session.add(row)
    await db_session.commit()

    await _detach_ipam_for_static(db_session, st)
    await db_session.flush()
    await db_session.refresh(row)

    # Freed to available (not allocated) so a new dynamic lease can reclaim it.
    assert row.status == "available"
    assert row.static_assignment_id is None
