"""#844 — overlapping IP spaces must not converge on shared DHCP/DNS infra.

IPAM deliberately allows the same CIDR in different IP spaces (VRF
semantics). What it must NOT allow is two same-prefix subnets reaching the
same DHCP server group (Kea rejects the whole config at load on a duplicate
prefix — an outage for every scope on the group) or the same DNS reverse
zone (two tenants' PTRs fold into one RRset — cross-tenant hostname
disclosure). These tests pin the guards at each convergence point.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServer, DHCPServerGroup
from app.models.dns import DNSRecord, DNSServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.dhcp.config_bundle import build_config_bundle
from app.services.dhcp.pull_leases import _find_containing_subnet
from app.services.dns.reverse_zone import ensure_reverse_zone_for_subnet

CIDR = "192.168.1.0/24"


async def _make_token(db: AsyncSession) -> str:
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


async def _space_subnet(db: AsyncSession, cidr: str = CIDR) -> Subnet:
    """One space holding one subnet — call twice for the overlap scenario."""
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=cidr, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=cidr, name="s")
    db.add(subnet)
    await db.flush()
    return subnet


# ── Guard 1: scope create / activate refuses overlapping CIDR ───────────────


@pytest.mark.asyncio
async def test_second_space_same_cidr_same_group_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _make_token(db_session)
    sub_a = await _space_subnet(db_session)
    sub_b = await _space_subnet(db_session)
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db_session.add(grp)
    await db_session.flush()
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        f"/api/v1/dhcp/subnets/{sub_a.id}/dhcp-scopes",
        headers=h,
        json={"group_id": str(grp.id), "name": "a"},
    )
    assert r.status_code == 201, r.text

    r = await client.post(
        f"/api/v1/dhcp/subnets/{sub_b.id}/dhcp-scopes",
        headers=h,
        json={"group_id": str(grp.id), "name": "b"},
    )
    assert r.status_code == 409, r.text
    assert "IP" in r.text and "space" in r.text.lower()


@pytest.mark.asyncio
async def test_second_space_same_cidr_separate_group_is_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The documented safe deployment: one group per overlapping space."""
    token = await _make_token(db_session)
    sub_a = await _space_subnet(db_session)
    sub_b = await _space_subnet(db_session)
    grp_a = DHCPServerGroup(name=f"ga-{uuid.uuid4().hex[:6]}")
    grp_b = DHCPServerGroup(name=f"gb-{uuid.uuid4().hex[:6]}")
    db_session.add_all([grp_a, grp_b])
    await db_session.flush()
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    for sub, grp in ((sub_a, grp_a), (sub_b, grp_b)):
        r = await client.post(
            f"/api/v1/dhcp/subnets/{sub.id}/dhcp-scopes",
            headers=h,
            json={"group_id": str(grp.id), "name": "s"},
        )
        assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_inactive_create_allowed_but_activation_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Activation is the other door into the rendered config."""
    token = await _make_token(db_session)
    sub_a = await _space_subnet(db_session)
    sub_b = await _space_subnet(db_session)
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db_session.add(grp)
    await db_session.flush()
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        f"/api/v1/dhcp/subnets/{sub_a.id}/dhcp-scopes",
        headers=h,
        json={"group_id": str(grp.id), "name": "a"},
    )
    assert r.status_code == 201, r.text

    # Inactive: never reaches the rendered config, so allowed…
    r = await client.post(
        f"/api/v1/dhcp/subnets/{sub_b.id}/dhcp-scopes",
        headers=h,
        json={"group_id": str(grp.id), "name": "b", "enabled": False},
    )
    assert r.status_code == 201, r.text
    scope_b = r.json()

    # …but flipping it on re-runs the guard.
    r = await client.put(f"/api/v1/dhcp/scopes/{scope_b['id']}", headers=h, json={"enabled": True})
    assert r.status_code == 409, r.text


# ── Guard 2: bundle build drops a raced-in duplicate prefix ─────────────────


@pytest.mark.asyncio
async def test_bundle_ships_one_scope_per_prefix_oldest_wins(
    db_session: AsyncSession,
) -> None:
    sub_a = await _space_subnet(db_session)
    sub_b = await _space_subnet(db_session)
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db_session.add(grp)
    await db_session.flush()
    srv = DHCPServer(
        name=f"kea-{uuid.uuid4().hex[:6]}",
        driver="kea",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    older = DHCPScope(
        subnet_id=sub_a.id,
        group_id=grp.id,
        name="older",
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = DHCPScope(
        subnet_id=sub_b.id,
        group_id=grp.id,
        name="newer",
        is_active=True,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add_all([srv, older, newer])
    await db_session.flush()

    bundle = await build_config_bundle(db_session, srv)
    cidrs = [s.subnet_cidr for s in bundle.scopes]
    assert cidrs.count(CIDR) == 1, cidrs
    # Oldest scope's subnet is the one shipped.
    assert len(bundle.scopes) == 1


# ── Guard 3: lease→subnet resolution prefers the server's own subnets ───────


def _fake_subnet(cidr: str) -> tuple[SimpleNamespace, ipaddress.IPv4Network]:
    return (SimpleNamespace(id=uuid.uuid4()), ipaddress.ip_network(cidr))


def test_find_containing_subnet_prefers_scoped_subnet() -> None:
    a = _fake_subnet(CIDR)
    b = _fake_subnet(CIDR)
    subnets = [a, b]
    # Preferring either side wins over an equal prefix from the other space.
    assert _find_containing_subnet("192.168.1.10", subnets, preferred_subnet_ids={b[0].id}) is b[0]
    assert _find_containing_subnet("192.168.1.10", subnets, preferred_subnet_ids={a[0].id}) is a[0]


def test_find_containing_subnet_unpreferred_tie_is_deterministic() -> None:
    a = _fake_subnet(CIDR)
    b = _fake_subnet(CIDR)
    first = _find_containing_subnet("192.168.1.10", [a, b])
    # Same winner regardless of input order.
    assert _find_containing_subnet("192.168.1.10", [b, a]) is first


def test_find_containing_subnet_longest_prefix_still_wins_within_preferred() -> None:
    wide = _fake_subnet("192.168.0.0/16")
    narrow = _fake_subnet(CIDR)
    got = _find_containing_subnet(
        "192.168.1.10",
        [wide, narrow],
        preferred_subnet_ids={wide[0].id, narrow[0].id},
    )
    assert got is narrow[0]


# ── Guard 4: reverse zone is never shared across spaces ─────────────────────


@pytest.mark.asyncio
async def test_reverse_zone_not_shared_across_spaces(
    db_session: AsyncSession,
) -> None:
    sub_a = await _space_subnet(db_session)
    sub_b = await _space_subnet(db_session)
    g = DNSServerGroup(name=f"dg-{uuid.uuid4().hex[:6]}")
    db_session.add(g)
    await db_session.flush()

    zone_a = await ensure_reverse_zone_for_subnet(db_session, sub_a, None, dns_group_id=g.id)
    assert zone_a is not None
    assert zone_a.linked_subnet_id == sub_a.id

    # Same CIDR, different space, same group → refused, not silently shared.
    zone_b = await ensure_reverse_zone_for_subnet(db_session, sub_b, None, dns_group_id=g.id)
    assert zone_b is None

    # Idempotent for the owning subnet itself.
    again = await ensure_reverse_zone_for_subnet(db_session, sub_a, None, dns_group_id=g.id)
    assert again is not None and again.id == zone_a.id


@pytest.mark.asyncio
async def test_resolve_reverse_zone_skips_other_space_zone(
    db_session: AsyncSession,
) -> None:
    from app.api.v1.ipam.router import _resolve_reverse_zone

    sub_a = await _space_subnet(db_session)
    sub_b = await _space_subnet(db_session)
    g = DNSServerGroup(name=f"dg-{uuid.uuid4().hex[:6]}")
    db_session.add(g)
    await db_session.flush()
    zone_a = await ensure_reverse_zone_for_subnet(db_session, sub_a, None, dns_group_id=g.id)
    assert zone_a is not None

    # Subnet B resolves DNS through the same group (the MSP misconfig).
    sub_b.dns_group_ids = [str(g.id)]
    await db_session.flush()

    ip = ipaddress.ip_address("192.168.1.10")
    assert await _resolve_reverse_zone(db_session, sub_b, ip) is None
    # The owning space still resolves its own zone.
    sub_a.dns_group_ids = [str(g.id)]
    await db_session.flush()
    got = await _resolve_reverse_zone(db_session, sub_a, ip)
    assert got is not None and got.id == zone_a.id


@pytest.mark.asyncio
async def test_ptr_collision_warning_on_manual_record(
    db_session: AsyncSession,
) -> None:
    """A manual PTR at the same name surfaces as a warning, not a silent stack."""
    from app.api.v1.ipam.router import _check_ip_collisions

    sub_a = await _space_subnet(db_session)
    g = DNSServerGroup(name=f"dg-{uuid.uuid4().hex[:6]}")
    db_session.add(g)
    await db_session.flush()
    zone = await ensure_reverse_zone_for_subnet(db_session, sub_a, None, dns_group_id=g.id)
    assert zone is not None
    sub_a.dns_group_ids = [str(g.id)]
    db_session.add(
        DNSRecord(
            zone_id=zone.id,
            name="10",
            fqdn="10.1.168.192.in-addr.arpa.",
            record_type="PTR",
            value="host.other.example.com.",
        )
    )
    await db_session.flush()

    warnings = await _check_ip_collisions(
        db_session,
        hostname="mine",
        forward_zone_id=None,
        mac_address=None,
        subnet=sub_a,
        address="192.168.1.10",
    )
    kinds = [w["kind"] for w in warnings]
    assert "ptr_collision" in kinds, warnings
