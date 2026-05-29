"""DHCPv6 stateful / stateless / SLAAC mode — Kea render + scope API (#52)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.drivers.dhcp.base import (
    ConfigBundle,
    PoolDef,
    ScopeDef,
    ServerOptionsDef,
    StaticAssignmentDef,
)
from app.drivers.dhcp.kea import KeaDriver
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet

V6_CIDR = "2001:db8:52::/64"


def _v6_scope(mode: str) -> ScopeDef:
    return ScopeDef(
        subnet_cidr=V6_CIDR,
        address_family="ipv6",
        v6_address_mode=mode,
        lease_time=3600,
        options={"dns-servers": ["2001:db8::53"]},
        pools=(PoolDef(start_ip="2001:db8:52::100", end_ip="2001:db8:52::1ff"),),
        statics=(
            StaticAssignmentDef(ip_address="2001:db8:52::5", mac_address="aa:bb:cc:dd:ee:ff"),
        ),
    )


def _bundle(scope: ScopeDef) -> ConfigBundle:
    return ConfigBundle(
        server_id="00000000-0000-0000-0000-000000000000",
        server_name="kea6-test",
        driver="kea",
        roles=(),
        options=ServerOptionsDef(options={}, lease_time=3600),
        scopes=(scope,),
        client_classes=(),
        generated_at=datetime.now(UTC),
    )


def _render_subnet6(mode: str) -> dict:
    cfg = json.loads(KeaDriver().render_config(_bundle(_v6_scope(mode))))
    return cfg["Dhcp6"]["subnet6"][0]


# ── Kea render by mode ──────────────────────────────────────────────────


def test_stateful_renders_pools_and_options() -> None:
    s = _render_subnet6("stateful")
    assert len(s["pools"]) == 1  # address pool present
    assert "option-data" in s  # options served
    assert len(s["reservations"]) == 1


def test_stateless_drops_pools_keeps_options() -> None:
    s = _render_subnet6("stateless")
    assert s["pools"] == []  # no address assignment
    assert "option-data" in s  # but options still served (Information-Request)
    assert len(s["reservations"]) == 1


def test_slaac_drops_pools_and_options() -> None:
    s = _render_subnet6("slaac")
    assert s["pools"] == []
    assert "option-data" not in s  # router's RA does everything
    assert s["reservations"] == []  # no DHCP role at all


def test_v4_scope_ignores_mode() -> None:
    # A v4 scope with a (meaningless) non-stateful mode still serves pools.
    sc = ScopeDef(
        subnet_cidr="192.0.2.0/24",
        address_family="ipv4",
        v6_address_mode="slaac",
        pools=(PoolDef(start_ip="192.0.2.10", end_ip="192.0.2.50"),),
        options={"routers": ["192.0.2.1"]},
    )
    cfg = json.loads(KeaDriver().render_config(_bundle(sc)))
    sub = cfg["Dhcp4"]["subnet4"][0]
    assert len(sub["pools"]) == 1
    assert "option-data" in sub


def test_v6_mode_shifts_etag() -> None:
    # Changing the v6 mode must change the bundle ETag so agents re-pull.
    assert (
        _bundle(_v6_scope("stateful")).compute_etag()
        != _bundle(_v6_scope("stateless")).compute_etag()
    )


# ── Scope API ───────────────────────────────────────────────────────────


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


async def _v6_subnet_and_group(db: AsyncSession) -> tuple[Subnet, DHCPServerGroup]:
    space = IPSpace(name=f"v6-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=V6_CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=V6_CIDR, name="s")
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, grp])
    await db.flush()
    return subnet, grp


async def test_scope_api_persists_v6_mode(client: AsyncClient, db_session: AsyncSession) -> None:
    token = await _make_user(db_session)
    subnet, grp = await _v6_subnet_and_group(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers=h,
        json={
            "group_id": str(grp.id),
            "name": "v6-scope",
            "v6_address_mode": "stateless",
            "ra_managed_flag": False,
            "ra_other_flag": True,
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["address_family"] == "ipv6"
    assert body["v6_address_mode"] == "stateless"
    assert body["ra_managed_flag"] is False
    assert body["ra_other_flag"] is True

    scope_id = body["id"]
    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope_id}",
        headers=h,
        json={"v6_address_mode": "slaac"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["v6_address_mode"] == "slaac"

    scope = await db_session.get(DHCPScope, uuid.UUID(scope_id))
    await db_session.refresh(scope)
    assert scope.v6_address_mode == "slaac"


async def test_scope_api_rejects_bad_mode(client: AsyncClient, db_session: AsyncSession) -> None:
    token = await _make_user(db_session)
    subnet, grp = await _v6_subnet_and_group(db_session)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers={"Authorization": f"Bearer {token}"},
        json={"group_id": str(grp.id), "name": "bad", "v6_address_mode": "bogus"},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("mode", ["stateful", "stateless", "slaac"])
def test_all_modes_render_valid_subnet(mode: str) -> None:
    # Every mode must produce a syntactically present subnet6 entry with the
    # CIDR + lifetime, regardless of pools/options.
    s = _render_subnet6(mode)
    assert s["subnet"] == V6_CIDR
    assert s["valid-lifetime"] == 3600
