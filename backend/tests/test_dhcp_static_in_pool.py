"""In-pool static reservations + manual IPAM allocation (#631).

The old rule refused a static reservation (409) or a manual IPAM allocation
(422) whose IP fell inside a `dynamic` pool. That rule had no driver basis —
pinning a MAC inside the range is the standard idiom on Kea / FortiGate /
Windows — so it's gone: reservations are allowed unconditionally, and manual
IPAM allocation downgrades to a force-overridable soft-collision warning.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPPool, DHCPScope, DHCPServer, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet

NETWORK = "10.70.0.0/24"
POOL_START = "10.70.0.100"
POOL_END = "10.70.0.150"
IN_POOL_IP = "10.70.0.120"
OUT_OF_POOL_IP = "10.70.0.20"


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"ip-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="In-Pool Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _scope_with_dynamic_pool(db: AsyncSession) -> tuple[Subnet, DHCPScope]:
    space = IPSpace(name=f"ip-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="root")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=NETWORK, name="lan")
    group = DHCPServerGroup(name=f"ip-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, group])
    await db.flush()
    db.add(
        DHCPServer(
            name=f"ip-{uuid.uuid4().hex[:6]}",
            driver="kea",
            host="127.0.0.1",
            server_group_id=group.id,
        )
    )
    scope = DHCPScope(group_id=group.id, subnet_id=subnet.id, name="scope", address_family="ipv4")
    db.add(scope)
    await db.flush()
    db.add(
        DHCPPool(
            scope_id=scope.id,
            name="dyn",
            start_ip=POOL_START,
            end_ip=POOL_END,
            pool_type="dynamic",
        )
    )
    await db.flush()
    await db.commit()
    return subnet, scope


@pytest.mark.asyncio
async def test_static_reservation_inside_dynamic_pool_is_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin(db_session)
    _, scope = await _scope_with_dynamic_pool(db_session)

    resp = await client.post(
        f"/api/v1/dhcp/scopes/{scope.id}/statics",
        headers=headers,
        json={"ip_address": IN_POOL_IP, "mac_address": "aa:bb:cc:dd:ee:01"},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_static_reservation_outside_subnet_still_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The neighbouring subnet-CIDR containment check (#619) must survive — only
    # the in-pool rule was removed.
    headers = await _admin(db_session)
    _, scope = await _scope_with_dynamic_pool(db_session)

    resp = await client.post(
        f"/api/v1/dhcp/scopes/{scope.id}/statics",
        headers=headers,
        json={"ip_address": "10.99.0.5", "mac_address": "aa:bb:cc:dd:ee:02"},
    )
    assert resp.status_code == 422, resp.text
    assert "outside the scope's subnet" in resp.text


@pytest.mark.asyncio
async def test_manual_ipam_allocation_in_pool_warns_then_forces(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin(db_session)
    subnet, _ = await _scope_with_dynamic_pool(db_session)

    body = {"address": IN_POOL_IP, "hostname": "printer1", "status": "allocated"}
    # First attempt (force implicit false) → soft collision warning, no write.
    warn = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses", headers=headers, json=body
    )
    assert warn.status_code == 409, warn.text
    detail = warn.json()["detail"]
    assert detail["requires_confirmation"] is True
    pool_warnings = [w for w in detail["warnings"] if w.get("kind") == "dynamic_pool"]
    assert len(pool_warnings) == 1
    assert pool_warnings[0]["address"] == IN_POOL_IP
    assert pool_warnings[0]["pool_start"] == POOL_START
    assert pool_warnings[0]["pool_end"] == POOL_END

    # Re-submit with force=true → the allocation goes through.
    forced = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={**body, "force": True},
    )
    assert forced.status_code == 201, forced.text
    assert forced.json()["address"] == IN_POOL_IP


@pytest.mark.asyncio
async def test_manual_ipam_allocation_outside_pool_has_no_warning(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Sanity: a normal out-of-pool allocation is unaffected — no warning, 201.
    headers = await _admin(db_session)
    subnet, _ = await _scope_with_dynamic_pool(db_session)

    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={"address": OUT_OF_POOL_IP, "hostname": "server1", "status": "allocated"},
    )
    assert resp.status_code == 201, resp.text
