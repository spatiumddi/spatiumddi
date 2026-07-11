"""Tests for the PAN-OS reconciler + DAG enforcement (#605).

Stub ``PANOSClient`` so we don't need a real firewall. Validates:
* address objects/groups → ``FirewallObject`` mirror rows + IPAM resolve link;
* NAT rules → ``nat_mapping`` provenance rows;
* interface CIDRs → PAN-owned subnets, DHCP leases → PAN-owned addresses;
* disappeared-object delete;
* the DAG-enforcement reconciler registers/unregisters IP→tag.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.block_sync import NetworkBlock, NetworkBlockPush
from app.models.ipam import IPAddress, IPSpace, Subnet
from app.models.panos import FirewallObject, PANOSFirewall
from app.services.panos.client import (
    _PANAddressObject,
    _PANInterface,
    _PANLease,
    _PANNatRule,
    _PANRegisteredIP,
    _PANSystemInfo,
)
from app.services.panos.reconcile import reconcile_firewall

# ── Fixtures ─────────────────────────────────────────────────────────


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"panos-test-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_fw(
    db: AsyncSession,
    space: IPSpace,
    *,
    mirror_interfaces: bool = False,
    mirror_dhcp_leases: bool = False,
    block_sync_enabled: bool = False,
) -> PANOSFirewall:
    fw = PANOSFirewall(
        name=f"pa-{uuid.uuid4().hex[:6]}",
        host="pa.example.test",
        port=443,
        verify_tls=False,
        api_key_encrypted=encrypt_str("APIKEY"),
        ipam_space_id=space.id,
        mirror_interfaces=mirror_interfaces,
        mirror_dhcp_leases=mirror_dhcp_leases,
        block_sync_enabled=block_sync_enabled,
        block_sync_api_key_encrypted=encrypt_str("WRITEKEY") if block_sync_enabled else b"",
    )
    db.add(fw)
    await db.flush()
    return fw


class _FakeClient:
    def __init__(
        self,
        *,
        objects: list[_PANAddressObject] | None = None,
        groups: list[_PANAddressObject] | None = None,
        nat: list[_PANNatRule] | None = None,
        interfaces: list[_PANInterface] | None = None,
        leases: list[_PANLease] | None = None,
        registered: list[_PANRegisteredIP] | None = None,
    ) -> None:
        self.objects = objects or []
        self.groups = groups or []
        self.nat = nat or []
        self.interfaces = interfaces or []
        self.leases = leases or []
        self.registered = registered or []
        self.registered_calls: list[tuple[str, str]] = []
        self.unregistered_calls: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_system_info(self):
        return _PANSystemInfo(version="11.0.2", model="PA-VM", hostname="pa", serial="0001")

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

    async def list_registered_ips(self, tag=None):
        return self.registered

    async def register_ip_tag(self, ip, tag):
        self.registered_calls.append((ip, tag))

    async def unregister_ip_tag(self, ip, tag):
        self.unregistered_calls.append((ip, tag))


def _patch_client(fake: _FakeClient):
    return patch("app.services.panos.reconcile.PANOSClient", side_effect=lambda **_kw: fake)


# ── Mirror tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_address_objects_mirrored_and_resolved(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space)
    # Create an IPAM address the object should resolve to.
    from app.models.ipam import IPBlock

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
            _PANAddressObject("web", "host", "10.0.0.5/32", "web host", ["pci"]),
            _PANAddressObject("ext", "fqdn", "svc.example.com", "", []),
        ],
        groups=[_PANAddressObject("grp", "group", "web, ext", "", [])],
    )
    with _patch_client(fake):
        summary = await reconcile_firewall(db_session, fw)

    assert summary.ok, summary.error
    assert summary.objects_created == 3
    rows = (
        (
            await db_session.execute(
                select(FirewallObject).where(FirewallObject.panos_firewall_id == fw.id)
            )
        )
        .scalars()
        .all()
    )
    by_name = {r.name: r for r in rows}
    assert by_name["web"].kind == "host"
    assert by_name["web"].ip_address_id is not None  # resolved to the IPAM row
    assert by_name["web"].tags == ["pci"]
    assert by_name["ext"].resolved_cidr is None  # fqdn doesn't resolve
    assert by_name["grp"].kind == "group"


@pytest.mark.asyncio
async def test_disappeared_object_deleted(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space)
    await db_session.commit()

    with _patch_client(
        _FakeClient(objects=[_PANAddressObject("a", "host", "10.0.0.1/32", "", [])])
    ):
        await reconcile_firewall(db_session, fw)
    assert (
        await db_session.scalar(select(FirewallObject).where(FirewallObject.name == "a"))
    ) is not None

    # Next sync: object 'a' is gone upstream → row removed.
    with _patch_client(_FakeClient(objects=[])):
        summary = await reconcile_firewall(db_session, fw)
    assert summary.objects_deleted == 1
    assert (
        await db_session.scalar(select(FirewallObject).where(FirewallObject.name == "a"))
    ) is None


@pytest.mark.asyncio
async def test_nat_rules_mirrored_to_nat_mapping(db_session: AsyncSession) -> None:
    from app.models.ipam import NATMapping

    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        nat=[
            _PANNatRule(
                name="inbound-web",
                kind="1to1",
                source="any",
                original_dst="203.0.113.10",
                translated_dst="10.0.0.5",
                translated_src=None,
                description="port-forward",
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_firewall(db_session, fw)

    assert summary.nat_created == 1
    row = (
        await db_session.execute(select(NATMapping).where(NATMapping.panos_firewall_id == fw.id))
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
            _PANInterface(name="ethernet1/1", cidr="10.0.0.0/24", address="10.0.0.1", zone="trust")
        ],
        leases=[
            _PANLease(
                address="10.0.0.50", mac="aa:bb:cc:dd:ee:ff", hostname="laptop", state="active"
            )
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_firewall(db_session, fw)

    assert summary.ok, summary.error
    assert summary.subnets_created == 1
    sub = (
        await db_session.execute(select(Subnet).where(Subnet.panos_firewall_id == fw.id))
    ).scalar_one()
    assert str(sub.network) == "10.0.0.0/24"
    lease = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.panos_firewall_id == fw.id, IPAddress.address == "10.0.0.50"
            )
        )
    ).scalar_one()
    assert lease.status == "dhcp"


@pytest.mark.asyncio
async def test_interfaces_multi_range_get_containing_wrappers(
    db_session: AsyncSession,
) -> None:
    """Each mirrored subnet must be parented on a block that contains it — a
    single shared wrapper would strand the 192.168 subnet under a 10.x block."""
    from app.models.ipam import IPBlock

    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space, mirror_interfaces=True)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[
            _PANInterface(name="e1/1", cidr="10.1.0.0/24", address="10.1.0.1", zone="trust"),
            _PANInterface(name="e1/2", cidr="192.168.5.0/24", address="192.168.5.1", zone="dmz"),
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_firewall(db_session, fw)

    assert summary.ok, summary.error
    assert summary.subnets_created == 2
    subs = (
        (await db_session.execute(select(Subnet).where(Subnet.panos_firewall_id == fw.id)))
        .scalars()
        .all()
    )
    import ipaddress as _ip

    for s in subs:
        block = await db_session.get(IPBlock, s.block_id)
        assert block is not None
        # The parent block's network must contain the subnet's network.
        assert _ip.ip_network(str(s.network)).subnet_of(_ip.ip_network(str(block.network)))


@pytest.mark.asyncio
async def test_mirror_toggle_off_sweeps_previous_rows(db_session: AsyncSession) -> None:
    """Disabling a mirror toggle must clean up rows a prior enabled sync made,
    not strand them."""
    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space)
    await db_session.commit()

    with _patch_client(
        _FakeClient(objects=[_PANAddressObject("a", "host", "10.0.0.1/32", "", [])])
    ):
        await reconcile_firewall(db_session, fw)
    assert (
        await db_session.scalar(select(FirewallObject).where(FirewallObject.name == "a"))
    ) is not None

    # Operator turns the address-object mirror off; the row must be swept.
    fw.mirror_address_objects = False
    await db_session.commit()
    with _patch_client(_FakeClient()):
        summary = await reconcile_firewall(db_session, fw)
    assert summary.objects_deleted == 1
    assert (
        await db_session.scalar(select(FirewallObject).where(FirewallObject.name == "a"))
    ) is None


# ── Enforcement tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_panos_registers_active_ip(db_session: AsyncSession) -> None:
    from app.services.block_sync.reconcile import reconcile_panos

    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space, block_sync_enabled=True)
    fw.block_tag_name = "spatiumddi-quarantine"
    db_session.add(NetworkBlock(kind="ip", value="10.0.0.9", enabled=True))
    await db_session.commit()

    fake = _FakeClient(registered=[])  # nothing on device yet
    with patch("app.services.block_sync.reconcile.PANOSClient", side_effect=lambda **_kw: fake):
        summary = await reconcile_panos(db_session, fw)
    await db_session.commit()

    assert summary.ok, summary.error
    assert summary.added == 1
    assert ("10.0.0.9", "spatiumddi-quarantine") in fake.registered_calls
    push = (
        await db_session.execute(
            select(NetworkBlockPush).where(
                NetworkBlockPush.target_kind == "paloalto", NetworkBlockPush.target_id == fw.id
            )
        )
    ).scalar_one()
    assert push.push_status == "pushed"


@pytest.mark.asyncio
async def test_reconcile_panos_unregisters_lifted_ip(db_session: AsyncSession) -> None:
    from app.services.block_sync.reconcile import reconcile_panos

    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space, block_sync_enabled=True)
    fw.block_tag_name = "spatiumddi-quarantine"
    # A disabled (lifted) block whose push row still exists + still on device.
    block = NetworkBlock(kind="ip", value="10.0.0.9", enabled=False)
    db_session.add(block)
    await db_session.flush()
    db_session.add(
        NetworkBlockPush(
            block_id=block.id, target_kind="paloalto", target_id=fw.id, push_status="pushed"
        )
    )
    await db_session.commit()

    fake = _FakeClient(registered=[_PANRegisteredIP(ip="10.0.0.9", tags=["spatiumddi-quarantine"])])
    with patch("app.services.block_sync.reconcile.PANOSClient", side_effect=lambda **_kw: fake):
        summary = await reconcile_panos(db_session, fw)
    await db_session.commit()

    assert summary.removed == 1
    assert ("10.0.0.9", "spatiumddi-quarantine") in fake.unregistered_calls
    remaining = (
        (
            await db_session.execute(
                select(NetworkBlockPush).where(NetworkBlockPush.target_id == fw.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


@pytest.mark.asyncio
async def test_panos_config_error_rejects_panorama(db_session: AsyncSession) -> None:
    from app.services.block_sync.reconcile import panos_config_error

    space = await _make_space(db_session)
    fw = await _make_fw(db_session, space, block_sync_enabled=True)
    fw.is_panorama = True
    fw.block_tag_name = "spatiumddi-quarantine"
    err = panos_config_error(fw)
    assert err is not None and "Panorama" in err
