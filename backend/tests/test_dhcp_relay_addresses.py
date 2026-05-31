"""DHCP relay-agent config — Kea relay.ip-addresses render + scope API (#337)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.drivers.dhcp.base import (
    ConfigBundle,
    PoolDef,
    ScopeDef,
    ServerOptionsDef,
)
from app.drivers.dhcp.kea import KeaDriver
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet

V4_CIDR = "10.20.0.0/24"
V6_CIDR = "2001:db8:337::/64"
RELAYS = ["10.20.0.1", "192.0.2.250"]


def _bundle(scope: ScopeDef) -> ConfigBundle:
    return ConfigBundle(
        server_id="00000000-0000-0000-0000-000000000000",
        server_name="kea-relay-test",
        driver="kea",
        roles=(),
        options=ServerOptionsDef(options={}, lease_time=3600),
        scopes=(scope,),
        client_classes=(),
        generated_at=datetime.now(UTC),
    )


# ── Kea driver render ────────────────────────────────────────────────────


def test_v4_scope_emits_relay_block() -> None:
    sc = ScopeDef(
        subnet_cidr=V4_CIDR,
        relay_addresses=tuple(RELAYS),
        pools=(PoolDef(start_ip="10.20.0.100", end_ip="10.20.0.200"),),
    )
    cfg = json.loads(KeaDriver().render_config(_bundle(sc)))
    sub = cfg["Dhcp4"]["subnet4"][0]
    assert sub["relay"] == {"ip-addresses": RELAYS}


def test_v6_scope_emits_relay_block() -> None:
    sc = ScopeDef(
        subnet_cidr=V6_CIDR,
        address_family="ipv6",
        relay_addresses=("2001:db8:337::1",),
        pools=(PoolDef(start_ip="2001:db8:337::100", end_ip="2001:db8:337::1ff"),),
    )
    cfg = json.loads(KeaDriver().render_config(_bundle(sc)))
    sub = cfg["Dhcp6"]["subnet6"][0]
    assert sub["relay"] == {"ip-addresses": ["2001:db8:337::1"]}


def test_no_relay_block_when_empty() -> None:
    sc = ScopeDef(subnet_cidr=V4_CIDR, pools=())
    cfg = json.loads(KeaDriver().render_config(_bundle(sc)))
    assert "relay" not in cfg["Dhcp4"]["subnet4"][0]


def test_relay_change_shifts_etag() -> None:
    base = ScopeDef(subnet_cidr=V4_CIDR)
    with_relay = ScopeDef(subnet_cidr=V4_CIDR, relay_addresses=("10.20.0.1",))
    assert _bundle(base).compute_etag() != _bundle(with_relay).compute_etag()


# ── Scope API round-trip ───────────────────────────────────────────────────


async def _make_user(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _subnet_and_group(db: AsyncSession) -> tuple[Subnet, DHCPServerGroup]:
    space = IPSpace(name=f"relay-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=V4_CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=V4_CIDR, name="s")
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, grp])
    await db.flush()
    return subnet, grp


async def test_scope_api_persists_relay_addresses(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _make_user(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers=h,
        json={
            "group_id": str(grp.id),
            "name": "relay-scope",
            # duplicate + reorderable input → validator de-dupes, preserves order
            "relay_addresses": ["10.20.0.1", "192.0.2.250", "10.20.0.1"],
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["relay_addresses"] == ["10.20.0.1", "192.0.2.250"]

    scope_id = body["id"]
    scope = await db_session.get(DHCPScope, uuid.UUID(scope_id))
    await db_session.refresh(scope)
    assert scope.relay_addresses == ["10.20.0.1", "192.0.2.250"]

    # PUT replaces the set; empty list clears it.
    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope_id}",
        headers=h,
        json={"relay_addresses": ["172.16.0.1"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["relay_addresses"] == ["172.16.0.1"]

    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope_id}",
        headers=h,
        json={"relay_addresses": []},
    )
    assert r.status_code == 200, r.text
    assert r.json()["relay_addresses"] == []


async def test_scope_api_rejects_bad_relay_address(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _make_user(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "group_id": str(grp.id),
            "name": "bad-relay",
            "relay_addresses": ["not-an-ip"],
        },
    )
    assert r.status_code == 422
