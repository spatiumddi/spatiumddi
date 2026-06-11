"""Resource-scoped API tokens (#374).

A token can bind to a single subnet / DNS zone; the binding only ever NARROWS
the owner. Covers in-scope allow, out-of-scope 403, the create-time
grant-exceeds-owner rejection, and unknown-resource 422.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, generate_api_token, hash_password
from app.models.auth import APIToken, User
from app.models.dns import DNSServerGroup, DNSZone
from app.models.ipam import IPBlock, IPSpace, Subnet


async def _make_user(db: AsyncSession, *, superadmin: bool) -> tuple[User, str]:
    user = User(
        username=f"tok-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Token User",
        hashed_password=hash_password("x"),
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_token(db: AsyncSession, owner: User, grants: list[dict] | None) -> str:
    raw, _prefix, token_hash = generate_api_token()
    db.add(
        APIToken(
            name=f"t-{uuid.uuid4().hex[:6]}",
            token_hash=token_hash,
            prefix=raw[:10],
            scope="user",
            scopes=[],
            resource_grants=grants,
            user_id=owner.id,
            created_by_user_id=owner.id,
            is_active=True,
        )
    )
    await db.flush()
    return raw


async def _make_subnet(db: AsyncSession, space: IPSpace, network: str) -> Subnet:
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name=f"b-{uuid.uuid4().hex[:5]}")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=network, name="sn", total_ips=254)
    db.add(subnet)
    await db.flush()
    return subnet


@pytest.mark.asyncio
async def test_subnet_token_in_scope_allow_out_of_scope_deny(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await _make_user(db_session, superadmin=True)
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(space)
    await db_session.flush()
    sub_x = await _make_subnet(db_session, space, "10.30.1.0/24")
    sub_y = await _make_subnet(db_session, space, "10.30.2.0/24")
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "write", "resource_type": "subnet", "resource_id": str(sub_x.id)}],
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {raw}"}

    # In-scope: create an IP in the bound subnet → 201.
    r_ok = await client.post(
        f"/api/v1/ipam/subnets/{sub_x.id}/addresses",
        headers=hdr,
        json={"address": "10.30.1.10", "status": "allocated", "hostname": "in-scope"},
    )
    assert r_ok.status_code == 201, r_ok.text

    # Out-of-scope: same token, different subnet → 403 even though the owner
    # (superadmin) could do it.
    r_deny = await client.post(
        f"/api/v1/ipam/subnets/{sub_y.id}/addresses",
        headers=hdr,
        json={"address": "10.30.2.10", "status": "allocated", "hostname": "out-of-scope"},
    )
    assert r_deny.status_code == 403, r_deny.text


@pytest.mark.asyncio
async def test_subnet_token_cannot_create_subnets(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await _make_user(db_session, superadmin=True)
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(space)
    await db_session.flush()
    sub_x = await _make_subnet(db_session, space, "10.31.1.0/24")
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "write", "resource_type": "subnet", "resource_id": str(sub_x.id)}],
    )
    await db_session.commit()

    r = await client.post(
        "/api/v1/ipam/subnets",
        headers={"Authorization": f"Bearer {raw}"},
        json={
            "space_id": str(space.id),
            "block_id": str(sub_x.block_id),
            "network": "10.31.9.0/24",
        },
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_dns_zone_token_other_zone_denied(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await _make_user(db_session, superadmin=True)
    group = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(group)
    await db_session.flush()
    zone_a = DNSZone(group_id=group.id, name=f"a-{uuid.uuid4().hex[:6]}.test", kind="forward")
    zone_b = DNSZone(group_id=group.id, name=f"b-{uuid.uuid4().hex[:6]}.test", kind="forward")
    db_session.add_all([zone_a, zone_b])
    await db_session.flush()
    raw = await _make_token(
        db_session,
        owner,
        [{"action": "write", "resource_type": "dns_zone", "resource_id": str(zone_a.id)}],
    )
    await db_session.commit()
    hdr = {"Authorization": f"Bearer {raw}"}

    # Listing records in the bound zone is allowed (read covered by write? no —
    # GET maps to read; the grant action is write. A write grant doesn't cover
    # read on the coarse gate, so list 403s — but record CREATE in the bound
    # zone is allowed). Verify the cross-zone create is denied.
    r_deny = await client.post(
        f"/api/v1/dns/groups/{group.id}/zones/{zone_b.id}/records",
        headers=hdr,
        json={"name": "www", "record_type": "A", "value": "10.0.0.1", "ttl": 300},
    )
    assert r_deny.status_code == 403, r_deny.text


@pytest.mark.asyncio
async def test_create_token_grant_exceeds_owner_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A non-superadmin with no subnet rights can't mint a subnet-write token."""
    _, token = await _make_user(db_session, superadmin=False)
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(space)
    await db_session.flush()
    sub = await _make_subnet(db_session, space, "10.32.1.0/24")
    await db_session.commit()

    r = await client.post(
        "/api/v1/api-tokens",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "scoped",
            "resource_grants": [
                {"action": "write", "resource_type": "subnet", "resource_id": str(sub.id)}
            ],
        },
    )
    # 403 — can't grant more than yourself. (A user with zero RBAC can't even
    # reach the api-tokens surface? api-tokens is self-service, so they can;
    # the grant validation is what rejects.)
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_create_token_unknown_resource_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, superadmin=True)
    await db_session.commit()
    r = await client.post(
        "/api/v1/api-tokens",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "scoped",
            "resource_grants": [
                {
                    "action": "write",
                    "resource_type": "subnet",
                    "resource_id": str(uuid.uuid4()),
                }
            ],
        },
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_create_token_bad_resource_type_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, superadmin=True)
    await db_session.commit()
    r = await client.post(
        "/api/v1/api-tokens",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "scoped",
            "resource_grants": [
                {"action": "write", "resource_type": "ip_block", "resource_id": str(uuid.uuid4())}
            ],
        },
    )
    # Shape validator rejects unsupported resource_type at the schema layer.
    assert r.status_code == 422, r.text
