"""Ansible dynamic-inventory endpoint tests (#67)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


async def _admin_token(db: AsyncSession) -> str:
    user = User(
        username=f"ans-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Ansible Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _space_block_subnet(
    db: AsyncSession, *, space_name: str, block_name: str, subnet_name: str, cidr: str
) -> Subnet:
    space = IPSpace(name=space_name, description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name=block_name)
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=cidr,
        name=subnet_name,
        total_ips=254,
    )
    db.add(subnet)
    await db.flush()
    return subnet


async def test_inventory_groups_hostvars_and_excludes_placeholders(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _admin_token(db_session)
    subnet = await _space_block_subnet(
        db_session,
        space_name="Prod Space",
        block_name="Core Block",
        subnet_name="web-subnet",
        cidr="10.0.0.0/24",
    )
    db_session.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.0.0.5",
            status="allocated",
            role="host",
            hostname="web01",
            tags={"role": "web", "tier": "prod"},
            custom_fields={"env": "prod"},
        )
    )
    # A placeholder network row must NOT surface as a manageable host.
    db_session.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.0.0.0",
            status="reserved",
            role="network",
        )
    )
    await db_session.commit()

    r = await client.get("/api/v1/ansible/inventory", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    inv = r.json()

    # Host present under its hostname with ansible_host = the IP.
    hv = inv["_meta"]["hostvars"]
    assert "web01" in hv
    assert hv["web01"]["ansible_host"] == "10.0.0.5"
    assert hv["web01"]["spatium_space"] == "Prod Space"
    assert "web01" in inv["all"]["hosts"]

    # Grouped by space / block / subnet / tag / custom-field (names sanitised).
    assert "web01" in inv["space_Prod_Space"]["hosts"]
    assert "web01" in inv["block_Core_Block"]["hosts"]
    assert "web01" in inv["subnet_web_subnet"]["hosts"]
    # Tags are key+value (a JSONB dict) — grouped by both so distinct
    # values don't collide and values aren't dropped.
    assert "web01" in inv["tag_role_web"]["hosts"]
    assert "web01" in inv["tag_tier_prod"]["hosts"]
    assert hv["web01"]["spatium_tags"] == {"role": "web", "tier": "prod"}
    assert "web01" in inv["cf_env_prod"]["hosts"]

    # Placeholder network row excluded entirely.
    assert "10.0.0.0" not in hv
    assert "10.0.0.0" not in inv["all"]["hosts"]


async def test_inventory_host_query_returns_single_hostvars(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _admin_token(db_session)
    subnet = await _space_block_subnet(
        db_session,
        space_name="S2",
        block_name="B2",
        subnet_name="s2",
        cidr="10.1.0.0/24",
    )
    db_session.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.1.0.9",
            status="allocated",
            role="host",
            hostname="h9",
        )
    )
    await db_session.commit()

    r = await client.get(
        "/api/v1/ansible/inventory?host=h9",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ansible_host"] == "10.1.0.9"
    assert "_meta" not in body  # --host returns just the vars
