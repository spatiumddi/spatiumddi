"""Tests for the OPNsense reconciler.

Stub ``OPNsenseClient`` so we don't need a real firewall. Validates
interface → subnet creation (real LAN semantics: gateway set, broadcast
counted out), DHCP lease + static reservation + ARP status mapping,
operator-row claim + user_modified_at lock, sibling-integration
ownership guard, disappeared-row delete, and cascade on firewall
removal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.opnsense import OPNsenseRouter
from app.services.opnsense.client import (
    _OPNArpEntry,
    _OPNFirmwareInfo,
    _OPNInterface,
    _OPNLease,
    _OPNReservation,
    _OPNVlan,
)
from app.services.opnsense.reconcile import reconcile_router

# ── Fixtures ─────────────────────────────────────────────────────────


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"opnsense-test-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_router(
    db: AsyncSession,
    space: IPSpace,
    *,
    mirror_dhcp_leases: bool = True,
    mirror_static_mappings: bool = True,
    mirror_arp: bool = False,
) -> OPNsenseRouter:
    router = OPNsenseRouter(
        name=f"fw-{uuid.uuid4().hex[:6]}",
        host="fw.example.test",
        port=443,
        verify_tls=False,
        api_key="KEY",
        # Encrypt a real secret so the reconciler's decrypt guard passes.
        api_secret_encrypted=_encrypt("SECRET"),
        ipam_space_id=space.id,
        mirror_dhcp_leases=mirror_dhcp_leases,
        mirror_static_mappings=mirror_static_mappings,
        mirror_arp=mirror_arp,
    )
    db.add(router)
    await db.flush()
    return router


def _encrypt(value: str) -> bytes:
    from app.core.crypto import encrypt_str  # noqa: PLC0415

    return encrypt_str(value)


class _FakeClient:
    def __init__(
        self,
        *,
        firmware: _OPNFirmwareInfo | None = None,
        interfaces: list[_OPNInterface] | None = None,
        vlans: list[_OPNVlan] | None = None,
        leases: list[_OPNLease] | None = None,
        reservations: list[_OPNReservation] | None = None,
        arp: list[_OPNArpEntry] | None = None,
    ) -> None:
        self.firmware = firmware or _OPNFirmwareInfo(version="OPNsense 24.7")
        self.interfaces = interfaces or []
        self.vlans = vlans or []
        self.leases = leases or []
        self.reservations = reservations or []
        self.arp = arp or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_firmware(self):
        return self.firmware

    async def list_interfaces(self):
        return self.interfaces

    async def list_vlans(self):
        return self.vlans

    async def list_leases(self):
        return self.leases

    async def list_reservations(self):
        return self.reservations

    async def list_arp(self):
        return self.arp


def _patch_client(fake: _FakeClient):
    def _ctor(**_kwargs):
        return fake

    return patch("app.services.opnsense.reconcile.OPNsenseClient", side_effect=_ctor)


def _iface(name: str, device: str, address: str, cidr: str, descr: str = "") -> _OPNInterface:
    return _OPNInterface(name=name, device=device, description=descr, cidr=cidr, address=address)


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interface_creates_subnet_with_gateway_and_lan_semantics(
    db_session: AsyncSession,
) -> None:
    """An OPNsense LAN interface is a real subnet: the firewall's
    interface IP is the gateway, and the /24 has 254 usable hosts
    (broadcast + network counted out)."""
    space = await _make_space(db_session)
    router = await _make_router(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[_iface("lan", "igb1", "10.0.0.1", "10.0.0.0/24", "LAN")],
    )
    with _patch_client(fake):
        summary = await reconcile_router(db_session, router)

    assert summary.ok, summary.error
    assert summary.subnets_created == 1

    sub = (
        await db_session.execute(select(Subnet).where(Subnet.opnsense_router_id == router.id))
    ).scalar_one()
    assert str(sub.network) == "10.0.0.0/24"
    assert str(sub.gateway) == "10.0.0.1"
    assert sub.total_ips == 254  # real LAN — broadcast + network out

    # The firewall interface IP lands as a reserved row.
    gw = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.opnsense_router_id == router.id,
                IPAddress.status == "reserved",
                IPAddress.address == "10.0.0.1",
            )
        )
    ).scalar_one()
    assert gw.hostname == router.name


@pytest.mark.asyncio
async def test_lease_and_reservation_status_mapping(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    router = await _make_router(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[_iface("lan", "igb1", "10.0.0.1", "10.0.0.0/24")],
        leases=[
            _OPNLease(
                address="10.0.0.50", mac="bc:24:11:e8:4a:3f", hostname="laptop", state="active"
            )
        ],
        reservations=[
            _OPNReservation(
                address="10.0.0.10", mac="aa:bb:cc:dd:ee:ff", hostname="printer", description="prn"
            )
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_router(db_session, router)
    assert summary.ok, summary.error

    lease_row = (
        await db_session.execute(select(IPAddress).where(IPAddress.address == "10.0.0.50"))
    ).scalar_one()
    assert lease_row.status == "dhcp"
    assert lease_row.auto_from_lease is True
    assert lease_row.hostname == "laptop"

    res_row = (
        await db_session.execute(select(IPAddress).where(IPAddress.address == "10.0.0.10"))
    ).scalar_one()
    assert res_row.status == "reserved"
    assert res_row.auto_from_lease is False
    assert res_row.hostname == "printer"


@pytest.mark.asyncio
async def test_arp_only_when_opted_in(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    router = await _make_router(db_session, space, mirror_arp=True)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[_iface("lan", "igb1", "10.0.0.1", "10.0.0.0/24")],
        arp=[
            _OPNArpEntry(
                address="10.0.0.77", mac="de:ad:be:ef:00:11", hostname="", interface="igb1"
            )
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_router(db_session, router)
    assert summary.ok, summary.error

    arp_row = (
        await db_session.execute(select(IPAddress).where(IPAddress.address == "10.0.0.77"))
    ).scalar_one()
    assert arp_row.status == "opnsense-arp"


@pytest.mark.asyncio
async def test_claims_operator_row_and_locks_soft_fields(db_session: AsyncSession) -> None:
    """A pre-existing operator IP at a desired address is claimed
    (FK stamped + user_modified_at set) and its operator-set hostname is
    NOT overwritten on subsequent reconciles."""
    space = await _make_space(db_session)
    router = await _make_router(db_session, space)
    # Operator-created block + subnet + IP at 10.0.0.50.
    block = IPBlock(space_id=space.id, network="10.0.0.0/24", name="op-block")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="op-subnet", total_ips=254
    )
    db_session.add(subnet)
    await db_session.flush()
    op_ip = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.50",
        status="allocated",
        hostname="operator-named",
    )
    db_session.add(op_ip)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[_iface("lan", "igb1", "10.0.0.1", "10.0.0.0/24")],
        leases=[
            _OPNLease(
                address="10.0.0.50", mac="bc:24:11:e8:4a:3f", hostname="dhcp-name", state="active"
            )
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_router(db_session, router)
    assert summary.ok, summary.error

    # The operator subnet at the same CIDR was matched, not duplicated.
    assert summary.subnets_matched == 1
    all_subnets = (
        (await db_session.execute(select(Subnet).where(Subnet.network == "10.0.0.0/24")))
        .scalars()
        .all()
    )
    assert len(all_subnets) == 1

    await db_session.refresh(op_ip)
    assert op_ip.opnsense_router_id == router.id  # claimed
    assert op_ip.user_modified_at is not None  # locked
    assert op_ip.hostname == "operator-named"  # NOT clobbered


@pytest.mark.asyncio
async def test_sibling_integration_row_not_claimed(db_session: AsyncSession) -> None:
    """A row already owned by another integration (here Proxmox) must
    not be claimed by the OPNsense reconciler."""
    space = await _make_space(db_session)
    router = await _make_router(db_session, space)
    block = IPBlock(space_id=space.id, network="10.0.0.0/24", name="op-block")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(
        space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="op-subnet", total_ips=254
    )
    db_session.add(subnet)
    await db_session.flush()
    foreign_node = uuid.uuid4()
    # Fabricate a row that looks proxmox-owned. We can't easily create a
    # real ProxmoxNode FK target without importing it, so stamp the FK
    # directly via a real ProxmoxNode.
    from app.models.proxmox import ProxmoxNode  # noqa: PLC0415

    pnode = ProxmoxNode(
        name=f"pve-{uuid.uuid4().hex[:6]}",
        host="pve.test",
        token_id="root@pam!x",
        ipam_space_id=space.id,
    )
    pnode.id = foreign_node
    db_session.add(pnode)
    await db_session.flush()
    foreign_ip = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.50",
        status="proxmox-vm",
        hostname="pve-guest",
        proxmox_node_id=pnode.id,
    )
    db_session.add(foreign_ip)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[_iface("lan", "igb1", "10.0.0.1", "10.0.0.0/24")],
        leases=[_OPNLease(address="10.0.0.50", mac=None, hostname="dhcp-name", state="active")],
    )
    with _patch_client(fake):
        summary = await reconcile_router(db_session, router)
    assert summary.ok, summary.error

    await db_session.refresh(foreign_ip)
    assert foreign_ip.opnsense_router_id is None  # untouched
    assert foreign_ip.proxmox_node_id == pnode.id
    assert any("another integration" in w for w in summary.warnings)


@pytest.mark.asyncio
async def test_disappeared_lease_is_deleted(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    router = await _make_router(db_session, space)
    await db_session.commit()

    iface = [_iface("lan", "igb1", "10.0.0.1", "10.0.0.0/24")]

    # First pass: lease at .50 created.
    with _patch_client(
        _FakeClient(
            interfaces=iface,
            leases=[_OPNLease(address="10.0.0.50", mac=None, hostname="x", state="active")],
        )
    ):
        await reconcile_router(db_session, router)
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.address == "10.0.0.50"))
    ).scalar_one_or_none() is not None

    # Second pass: lease gone → row deleted (no operator edits).
    with _patch_client(_FakeClient(interfaces=iface, leases=[])):
        summary = await reconcile_router(db_session, router)
    assert summary.addresses_deleted >= 1
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.address == "10.0.0.50"))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_router_cascades_ipam_rows(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    router = await _make_router(db_session, space)
    await db_session.commit()

    with _patch_client(
        _FakeClient(
            interfaces=[_iface("lan", "igb1", "10.0.0.1", "10.0.0.0/24")],
            leases=[_OPNLease(address="10.0.0.50", mac=None, hostname="x", state="active")],
        )
    ):
        await reconcile_router(db_session, router)

    rid = router.id
    await db_session.delete(router)
    await db_session.commit()

    # FK ON DELETE CASCADE sweeps the mirrored subnet + address rows.
    assert (
        await db_session.execute(select(Subnet).where(Subnet.opnsense_router_id == rid))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.opnsense_router_id == rid))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_vlan_label_in_subnet_description(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    router = await _make_router(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        interfaces=[_iface("opt1", "vlan0.20", "192.168.20.1", "192.168.20.0/24", "IoT")],
        vlans=[_OPNVlan(device="vlan0.20", parent="igb1", tag=20, description="IoT net")],
    )
    with _patch_client(fake):
        summary = await reconcile_router(db_session, router)
    assert summary.ok, summary.error

    sub = (
        await db_session.execute(select(Subnet).where(Subnet.network == "192.168.20.0/24"))
    ).scalar_one()
    assert "VLAN 20" in (sub.description or "")


@pytest.mark.asyncio
async def test_no_api_secret_records_error(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    router = OPNsenseRouter(
        name=f"fw-{uuid.uuid4().hex[:6]}",
        host="fw.test",
        api_key="KEY",
        api_secret_encrypted=b"",  # unset
        ipam_space_id=space.id,
    )
    db_session.add(router)
    await db_session.commit()

    summary = await reconcile_router(db_session, router)
    assert not summary.ok
    assert summary.error is not None
    await db_session.refresh(router)
    assert router.last_sync_error is not None
    assert router.last_synced_at is not None
    # Watermark is recent.
    assert (datetime.now(UTC) - router.last_synced_at).total_seconds() < 60
