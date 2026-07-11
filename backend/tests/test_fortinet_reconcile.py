"""Tests for the FortiGate reconciler (#606).

Stub ``FortinetClient`` so no real firewall is needed. Validates address
objects/groups → ``FirewallObject`` (owned by ``fortinet_firewall_id``), VIPs →
``nat_mapping``, interface CIDRs → subnets, DHCP leases → addresses, and the
disappeared-object sweep — mirroring ``test_panos_reconcile`` but through the
shared ``firewall_mirror`` engine.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.fortinet import FortinetFirewall
from app.models.ipam import IPAddress, IPBlock, IPSpace, NATMapping, Subnet
from app.models.panos import FirewallObject
from app.services.fortinet.client import (
    _FortiAddressObject,
    _FortiInterface,
    _FortiLease,
    _FortiNatRule,
    _FortiSystemInfo,
)
from app.services.fortinet.reconcile import reconcile_firewall


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"forti-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_fw(
    db: AsyncSession,
    space: IPSpace,
    *,
    mirror_interfaces: bool = False,
    mirror_dhcp_leases: bool = False,
) -> FortinetFirewall:
    fw = FortinetFirewall(
        name=f"fg-{uuid.uuid4().hex[:6]}",
        host="fg.example.test",
        port=443,
        verify_tls=False,
        api_token_encrypted=encrypt_str("TOK"),
        ipam_space_id=space.id,
        vdom="root",
        mirror_interfaces=mirror_interfaces,
        mirror_dhcp_leases=mirror_dhcp_leases,
    )
    db.add(fw)
    await db.flush()
    return fw


class _FakeClient:
    def __init__(
        self, *, objects=None, groups=None, nat=None, interfaces=None, leases=None
    ) -> None:
        self.objects = objects or []
        self.groups = groups or []
        self.nat = nat or []
        self.interfaces = interfaces or []
        self.leases = leases or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_system_info(self):
        return _FortiSystemInfo(version="7.4.3", model="FortiGate-60F", hostname="fg", serial="X1")

    async def list_address_objects(self):
        return self.objects

    async def list_address_groups(self):
        return self.groups

    async def list_nat_rules(self):
        return self.nat

    async def list_interfaces(self):
        return self.interfaces

    async def list_dhcp_leases(self):
        return self.leases


def _patch(fake: _FakeClient):
    return patch("app.services.fortinet.reconcile.FortinetClient", side_effect=lambda **_k: fake)


@pytest.mark.asyncio
async def test_objects_mirrored_and_owned_by_fortinet(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space)
    block = IPBlock(space_id=space.id, network="10.0.0.0/24", name="b")
    db_session.add(block)
    await db_session.flush()
    sub = Subnet(
        space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="s", total_ips=254
    )
    db_session.add(sub)
    await db_session.flush()
    db_session.add(IPAddress(subnet_id=sub.id, address="10.0.0.5", status="reserved"))
    await db_session.commit()

    fake = _FakeClient(
        objects=[
            _FortiAddressObject("web", "host", "10.0.0.5/32", "web", ["pci"]),
            _FortiAddressObject("dns", "fqdn", "svc.example.com", "", []),
        ],
        groups=[_FortiAddressObject("grp", "group", "web, dns", "", [])],
    )
    with _patch(fake):
        summary = await reconcile_firewall(db_session, fw)

    assert summary.ok, summary.error
    assert summary.objects_created == 3
    rows = (
        (
            await db_session.execute(
                select(FirewallObject).where(FirewallObject.fortinet_firewall_id == fw.id)
            )
        )
        .scalars()
        .all()
    )
    by_name = {r.name: r for r in rows}
    assert by_name["web"].kind == "host"
    assert by_name["web"].ip_address_id is not None
    assert by_name["web"].source_kind == "fortinet"
    assert by_name["web"].panos_firewall_id is None
    assert by_name["dns"].resolved_cidr is None


@pytest.mark.asyncio
async def test_vip_mirrored_to_nat_mapping(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        nat=[
            _FortiNatRule(
                name="vip-web",
                kind="1to1",
                source="",
                original_dst="203.0.113.10",
                translated_dst="10.0.0.5",
                translated_src=None,
                description="web vip",
            )
        ]
    )
    with _patch(fake):
        summary = await reconcile_firewall(db_session, fw)

    assert summary.nat_created == 1
    row = (
        await db_session.execute(select(NATMapping).where(NATMapping.fortinet_firewall_id == fw.id))
    ).scalar_one()
    assert str(row.internal_ip) == "10.0.0.5"
    assert str(row.external_ip) == "203.0.113.10"


@pytest.mark.asyncio
async def test_interfaces_and_leases_mirrored(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space, mirror_interfaces=True, mirror_dhcp_leases=True)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[
            _FortiInterface(name="port1", cidr="10.0.0.0/24", address="10.0.0.1", zone="lan")
        ],
        leases=[
            _FortiLease(address="10.0.0.50", mac="aa:bb:cc:dd:ee:ff", hostname="pc", state="active")
        ],
    )
    with _patch(fake):
        summary = await reconcile_firewall(db_session, fw)

    assert summary.ok, summary.error
    assert summary.subnets_created == 1
    sub = (
        await db_session.execute(select(Subnet).where(Subnet.fortinet_firewall_id == fw.id))
    ).scalar_one()
    assert str(sub.network) == "10.0.0.0/24"
    lease = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.fortinet_firewall_id == fw.id, IPAddress.address == "10.0.0.50"
            )
        )
    ).scalar_one()
    assert lease.status == "dhcp"


@pytest.mark.asyncio
async def test_disappeared_object_deleted(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space)
    await db_session.commit()

    with _patch(_FakeClient(objects=[_FortiAddressObject("a", "host", "10.0.0.1/32", "", [])])):
        await reconcile_firewall(db_session, fw)
    assert (
        await db_session.scalar(select(FirewallObject).where(FirewallObject.name == "a"))
    ) is not None

    with _patch(_FakeClient(objects=[])):
        summary = await reconcile_firewall(db_session, fw)
    assert summary.objects_deleted == 1
    assert (
        await db_session.scalar(select(FirewallObject).where(FirewallObject.name == "a"))
    ) is None
