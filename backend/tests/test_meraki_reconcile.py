"""Tests for the Meraki reconciler + per-client block enforcement (#606).

Stub ``MerakiClient`` so no cloud org is needed. Validates VLANs → subnets,
DHCP fixed-IP reservations → addresses, policy objects → ``FirewallObject``,
1:1 NAT → ``nat_mapping`` (all owned by ``meraki_org_id``), and the block-sync
per-client Blocked enforcement.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.block_sync import NetworkBlock, NetworkBlockPush
from app.models.ipam import IPAddress, IPSpace, NATMapping, Subnet
from app.models.meraki import MerakiOrg
from app.models.panos import FirewallObject
from app.services.meraki.client import (
    _MerakiNatRule,
    _MerakiNetwork,
    _MerakiOrgInfo,
    _MerakiPolicyObject,
    _MerakiReservation,
    _MerakiVlan,
)
from app.services.meraki.reconcile import reconcile_org


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"mer-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_org(db: AsyncSession, space: IPSpace, *, block_sync: bool = False) -> MerakiOrg:
    org = MerakiOrg(
        name=f"org-{uuid.uuid4().hex[:6]}",
        org_id="123456",
        api_key_encrypted=encrypt_str("KEY"),
        ipam_space_id=space.id,
        block_sync_enabled=block_sync,
        block_sync_api_key_encrypted=encrypt_str("WKEY") if block_sync else b"",
    )
    db.add(org)
    await db.flush()
    return org


class _FakeClient:
    def __init__(
        self,
        *,
        networks=None,
        vlans=None,
        reservations=None,
        objects=None,
        nat=None,
        clients=None,
        find_map=None,
    ) -> None:
        self._networks = networks or []
        self._vlans = vlans or {}
        self._reservations = reservations or {}
        self._objects = objects or []
        self._nat = nat or {}
        self._clients = clients or {}
        self._find_map = find_map or {}
        self._nat_errors = {}
        self.policy_calls: list[tuple[str, str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_organization(self):
        return _MerakiOrgInfo(id="123456", name="Acme", url="https://dashboard")

    async def list_networks(self, network_ids=None):
        return self._networks

    async def list_vlans(self, network_id, network_name=""):
        return self._vlans.get(network_id, [])

    async def list_reservations(self, network_id):
        return self._reservations.get(network_id, [])

    async def list_vlans_and_reservations(self, network_id, network_name=""):
        return self._vlans.get(network_id, []), self._reservations.get(network_id, [])

    async def list_policy_objects(self):
        return self._objects

    async def list_nat_rules(self, network_id):
        from app.services.meraki.client import MerakiClientError

        if network_id in self._nat_errors:
            raise MerakiClientError("boom", status_code=self._nat_errors[network_id])
        return self._nat.get(network_id, [])

    async def list_clients(self, network_id, timespan_seconds=86400):
        return self._clients.get(network_id, [])

    async def find_client(self, network_id, mac):
        return self._find_map.get((network_id, mac))

    async def set_client_policy(self, network_id, client_id, device_policy):
        self.policy_calls.append((network_id, client_id, device_policy))


def _patch_reconcile(fake: _FakeClient):
    return patch("app.services.meraki.reconcile.MerakiClient", side_effect=lambda **_k: fake)


@pytest.mark.asyncio
async def test_meraki_mirror(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    org = await _make_org(db_session, space)
    await db_session.commit()

    net = _MerakiNetwork(id="N1", name="Main", product_types=["appliance"])
    fake = _FakeClient(
        networks=[net],
        vlans={"N1": [_MerakiVlan("N1", "Main", "10", "data", "192.168.10.0/24", "192.168.10.1")]},
        reservations={
            "N1": [_MerakiReservation("N1", "192.168.10.20", "aa:bb:cc:00:11:22", "printer")]
        },
        objects=[
            _MerakiPolicyObject("web", "host", "192.168.10.20", "", ["network"]),
            _MerakiPolicyObject("net", "network", "192.168.10.0/24", "", ["network"]),
        ],
        nat={
            "N1": [
                _MerakiNatRule("nat1", "1to1", "", "203.0.113.5", "192.168.10.20", None, "web 1:1")
            ]
        },
    )
    with _patch_reconcile(fake):
        summary = await reconcile_org(db_session, org)

    assert summary.ok, summary.error
    assert summary.network_count == 1
    # VLAN → subnet owned by meraki_org_id
    sub = (
        await db_session.execute(select(Subnet).where(Subnet.meraki_org_id == org.id))
    ).scalar_one()
    assert str(sub.network) == "192.168.10.0/24"
    # Reservation → address (status reserved)
    res = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.meraki_org_id == org.id, IPAddress.address == "192.168.10.20"
            )
        )
    ).scalar_one()
    assert res.status == "reserved"
    # Policy objects → FirewallObject owned by meraki
    objs = (
        (
            await db_session.execute(
                select(FirewallObject).where(FirewallObject.meraki_org_id == org.id)
            )
        )
        .scalars()
        .all()
    )
    assert {o.name for o in objs} == {"web", "net"}
    assert all(o.source_kind == "meraki" for o in objs)
    # NAT → nat_mapping
    nat = (
        await db_session.execute(select(NATMapping).where(NATMapping.meraki_org_id == org.id))
    ).scalar_one()
    assert str(nat.internal_ip) == "192.168.10.20"
    assert str(nat.external_ip) == "203.0.113.5"


@pytest.mark.asyncio
async def test_permanent_network_error_skips_that_network(db_session: AsyncSession) -> None:
    """A 403 on one network skips it (its rows drop) but the org still mirrors
    the healthy networks — one inaccessible network can't block the whole org."""
    space = await _make_space(db_session)
    org = await _make_org(db_session, space)
    await db_session.commit()

    n1 = _MerakiNetwork(id="N1", name="Main", product_types=["appliance"])
    n2 = _MerakiNetwork(id="N2", name="Branch", product_types=["appliance"])
    fake = _FakeClient(
        networks=[n1, n2],
        vlans={"N1": [_MerakiVlan("N1", "Main", "10", "d", "192.168.10.0/24", "192.168.10.1")]},
    )
    fake._nat_errors = {"N2": 403}  # key not scoped to N2
    with _patch_reconcile(fake):
        summary = await reconcile_org(db_session, org)

    assert summary.ok, summary.error
    assert summary.subnets_created == 1  # N1 still mirrored
    assert any("Branch" in w for w in summary.warnings)


@pytest.mark.asyncio
async def test_transient_network_error_aborts_org(db_session: AsyncSession) -> None:
    """A 5xx on one network aborts the whole org sync (NN#5 — never diff the
    shared owner-set against a partial fetch and sweep good rows)."""
    space = await _make_space(db_session)
    org = await _make_org(db_session, space)
    await db_session.commit()

    n1 = _MerakiNetwork(id="N1", name="Main", product_types=["appliance"])
    fake = _FakeClient(
        networks=[n1],
        vlans={"N1": [_MerakiVlan("N1", "Main", "10", "d", "192.168.10.0/24", "192.168.10.1")]},
    )
    fake._nat_errors = {"N1": 503}
    with _patch_reconcile(fake):
        summary = await reconcile_org(db_session, org)

    assert not summary.ok
    assert summary.error is not None


@pytest.mark.asyncio
async def test_meraki_enforcement_blocks_client(db_session: AsyncSession) -> None:
    from app.services.block_sync.reconcile import reconcile_meraki

    space = await _make_space(db_session)
    org = await _make_org(db_session, space, block_sync=True)
    db_session.add(NetworkBlock(kind="mac", value="aa:bb:cc:dd:ee:ff", enabled=True))
    await db_session.commit()

    net = _MerakiNetwork(id="N1", name="Main", product_types=["appliance"])
    fake = _FakeClient(networks=[net], find_map={("N1", "aa:bb:cc:dd:ee:ff"): "client-1"})
    with patch("app.services.block_sync.reconcile.MerakiClient", side_effect=lambda **_k: fake):
        summary = await reconcile_meraki(db_session, org)
    await db_session.commit()

    assert summary.ok, summary.error
    assert summary.added == 1
    assert ("N1", "client-1", "Blocked") in fake.policy_calls
    push = (
        await db_session.execute(
            select(NetworkBlockPush).where(
                NetworkBlockPush.target_kind == "meraki", NetworkBlockPush.target_id == org.id
            )
        )
    ).scalar_one()
    assert push.push_status == "pushed"


@pytest.mark.asyncio
async def test_meraki_enforcement_unblocks_lifted(db_session: AsyncSession) -> None:
    from app.services.block_sync.reconcile import reconcile_meraki

    space = await _make_space(db_session)
    org = await _make_org(db_session, space, block_sync=True)
    block = NetworkBlock(kind="mac", value="aa:bb:cc:dd:ee:ff", enabled=False)
    db_session.add(block)
    await db_session.flush()
    db_session.add(
        NetworkBlockPush(
            block_id=block.id, target_kind="meraki", target_id=org.id, push_status="pushed"
        )
    )
    await db_session.commit()

    net = _MerakiNetwork(id="N1", name="Main", product_types=["appliance"])
    fake = _FakeClient(networks=[net], find_map={("N1", "aa:bb:cc:dd:ee:ff"): "client-1"})
    with patch("app.services.block_sync.reconcile.MerakiClient", side_effect=lambda **_k: fake):
        summary = await reconcile_meraki(db_session, org)
    await db_session.commit()

    assert summary.removed == 1
    assert ("N1", "client-1", "Normal") in fake.policy_calls
    remaining = (
        (
            await db_session.execute(
                select(NetworkBlockPush).where(NetworkBlockPush.target_id == org.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []
