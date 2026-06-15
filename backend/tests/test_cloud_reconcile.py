"""Tests for the Cloud (AWS / Azure / GCP) reconciler.

Fully offline (tier-3 providers, no test account): ``get_connector`` is
monkeypatched to return a stub connector whose ``fetch_inventory``
returns a hand-built :class:`CloudInventory`. Validates VPC-CIDR →
IPBlock creation, the GCP CIDR-less-network per-subnet-block fallback,
subnet enclosure + routed-overlay semantics, instance-NIC mirroring,
idempotency, the ``user_modified_at`` soft-field lock, prune-on-removal,
and the credential-empty + connector-error error paths.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_dict
from app.models.cloud import CloudEndpoint
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.cloud import reconcile as reconcile_mod
from app.services.cloud.base import (
    CloudConnectorError,
    CloudInstance,
    CloudInventory,
    CloudLoadBalancer,
    CloudNetwork,
    CloudNic,
    CloudPublicIP,
    CloudSubnet,
)
from app.services.cloud.reconcile import reconcile_endpoint

# ── Fixtures ─────────────────────────────────────────────────────────


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"cloud-test-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_endpoint(
    db: AsyncSession,
    space: IPSpace,
    *,
    provider: str = "aws",
    creds: dict[str, str] | None = None,
    mirror_load_balancers: bool = True,
    mirror_stopped_instances: bool = False,
    public_space_id: uuid.UUID | None = None,
) -> CloudEndpoint:
    endpoint = CloudEndpoint(
        name=f"cloud-{uuid.uuid4().hex[:6]}",
        provider=provider,
        credentials_encrypted=encrypt_dict(
            creds or {"access_key_id": "AKIA", "secret_access_key": "secret"}
        ),
        provider_config={},
        regions=["us-east-1"],
        ipam_space_id=space.id,
        public_space_id=public_space_id,
        mirror_load_balancers=mirror_load_balancers,
        mirror_stopped_instances=mirror_stopped_instances,
    )
    db.add(endpoint)
    await db.flush()
    return endpoint


class _FakeConnector:
    """Stub connector — returns a pre-built inventory (or raises)."""

    def __init__(
        self,
        *,
        inventory: CloudInventory | None = None,
        error: Exception | None = None,
    ) -> None:
        self._inventory = inventory
        self._error = error
        self.fetch_calls: list[dict] = []

    async def probe(self):  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def fetch_inventory(self, **kwargs):
        self.fetch_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._inventory is not None
        return self._inventory


def _patch_connector(fake: _FakeConnector):
    def _factory(provider, **_kwargs):  # noqa: ARG001
        return fake

    return patch.object(reconcile_mod, "get_connector", side_effect=_factory)


def _aws_inventory() -> CloudInventory:
    """A single-VPC AWS account with two subnets + two instances."""
    return CloudInventory(
        account_id="123456789012",
        networks=[
            CloudNetwork(
                id="vpc-1",
                name="prod-vpc",
                cidrs=("10.0.0.0/16",),
                region="us-east-1",
            )
        ],
        subnets=[
            CloudSubnet(
                id="subnet-a",
                name="app-a",
                network_id="vpc-1",
                cidr="10.0.1.0/24",
                region="us-east-1",
            ),
            CloudSubnet(
                id="subnet-b",
                name="app-b",
                network_id="vpc-1",
                cidr="10.0.2.0/24",
                region="us-east-1",
                gateway="10.0.2.254",
            ),
        ],
        instances=[
            CloudInstance(
                id="i-aaa",
                name="web-1",
                running=True,
                region="us-east-1",
                nics=(CloudNic(private_ip="10.0.1.10", mac="0a:11:22:33:44:55"),),
            ),
            CloudInstance(
                id="i-bbb",
                name="db-1",
                running=True,
                region="us-east-1",
                nics=(CloudNic(private_ip="10.0.2.20"),),
            ),
        ],
        public_ips=[CloudPublicIP(address="203.0.113.10", name="eip-1", attached=True)],
        load_balancers=[
            CloudLoadBalancer(id="lb-1", name="web-lb", frontend_ips=("198.51.100.5",))
        ],
    )


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vpc_creates_block_subnets_and_instance_rows(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    fake = _FakeConnector(inventory=_aws_inventory())
    with _patch_connector(fake):
        summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok, summary.error
    assert summary.provider_account_id == "123456789012"
    assert summary.network_count == 1
    assert summary.instance_count == 2

    # One IPBlock for the VPC CIDR, owned by the endpoint.
    blocks = (
        (await db_session.execute(select(IPBlock).where(IPBlock.cloud_endpoint_id == endpoint.id)))
        .scalars()
        .all()
    )
    assert len(blocks) == 1
    assert str(blocks[0].network) == "10.0.0.0/16"
    assert blocks[0].name == "aws:prod-vpc"

    # Two subnets, both enclosed by the VPC block, routed-overlay semantics.
    subs = (
        (await db_session.execute(select(Subnet).where(Subnet.cloud_endpoint_id == endpoint.id)))
        .scalars()
        .all()
    )
    nets = {str(s.network) for s in subs}
    assert nets == {"10.0.1.0/24", "10.0.2.0/24"}
    for s in subs:
        assert s.block_id == blocks[0].id
        assert s.kubernetes_semantics is True

    # Gateway: derived first-usable-host for subnet-a, explicit for subnet-b.
    sub_a = next(s for s in subs if str(s.network) == "10.0.1.0/24")
    sub_b = next(s for s in subs if str(s.network) == "10.0.2.0/24")
    assert str(sub_a.gateway) == "10.0.1.1"
    assert str(sub_b.gateway) == "10.0.2.254"

    # Instance NIC private IPs land as cloud-instance rows in their subnets.
    inst_rows = (
        (
            await db_session.execute(
                select(IPAddress).where(
                    IPAddress.cloud_endpoint_id == endpoint.id,
                    IPAddress.status == "cloud-instance",
                )
            )
        )
        .scalars()
        .all()
    )
    by_addr = {str(r.address): r for r in inst_rows}
    assert set(by_addr) == {"10.0.1.10", "10.0.2.20"}
    assert by_addr["10.0.1.10"].hostname == "web-1"
    assert (by_addr["10.0.1.10"].mac_address or "").lower() == "0a:11:22:33:44:55"
    assert by_addr["10.0.1.10"].subnet_id == sub_a.id

    # Public IP + LB frontend are out-of-band (no enclosing subnet) → skipped.
    assert summary.skipped_no_subnet >= 2
    pub_rows = (
        (
            await db_session.execute(
                select(IPAddress).where(IPAddress.status.in_(["cloud-public", "cloud-lb"]))
            )
        )
        .scalars()
        .all()
    )
    assert list(pub_rows) == []

    # The fetch was called with the endpoint's mirror policy.
    assert fake.fetch_calls == [{"include_stopped": False, "include_load_balancers": True}]


@pytest.mark.asyncio
async def test_gcp_cidrless_network_creates_per_subnet_blocks(
    db_session: AsyncSession,
) -> None:
    """GCP VPCs have no own CIDR — each subnet gets a block at its own CIDR."""
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space, provider="gcp")
    await db_session.commit()

    inv = CloudInventory(
        account_id="my-gcp-project",
        networks=[CloudNetwork(id="net-1", name="default", cidrs=(), region=None)],
        subnets=[
            CloudSubnet(
                id="s-1",
                name="us-central",
                network_id="net-1",
                cidr="10.128.0.0/20",
                region="us-central1",
            ),
            CloudSubnet(
                id="s-2",
                name="europe-west",
                network_id="net-1",
                cidr="10.132.0.0/20",
                region="europe-west1",
            ),
        ],
        instances=[
            CloudInstance(
                id="inst-1",
                name="gce-1",
                running=True,
                nics=(CloudNic(private_ip="10.128.0.5"),),
            )
        ],
    )
    with _patch_connector(_FakeConnector(inventory=inv)):
        summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok, summary.error
    # One block per subnet CIDR (no network-level CIDR block).
    blocks = (
        (await db_session.execute(select(IPBlock).where(IPBlock.cloud_endpoint_id == endpoint.id)))
        .scalars()
        .all()
    )
    assert {str(b.network) for b in blocks} == {"10.128.0.0/20", "10.132.0.0/20"}

    # Each subnet nests inside the block at its own CIDR.
    subs = (
        (await db_session.execute(select(Subnet).where(Subnet.cloud_endpoint_id == endpoint.id)))
        .scalars()
        .all()
    )
    by_net = {str(s.network): s for s in subs}
    block_by_net = {str(b.network): b for b in blocks}
    assert by_net["10.128.0.0/20"].block_id == block_by_net["10.128.0.0/20"].id

    # Instance row lands in the right subnet.
    ip = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.cloud_endpoint_id == endpoint.id,
                IPAddress.status == "cloud-instance",
            )
        )
    ).scalar_one()
    assert str(ip.address) == "10.128.0.5"
    assert ip.subnet_id == by_net["10.128.0.0/20"].id


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(db_session: AsyncSession) -> None:
    """A second run against the same inventory creates no duplicates."""
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    with _patch_connector(_FakeConnector(inventory=_aws_inventory())):
        first = await reconcile_endpoint(db_session, endpoint)
    with _patch_connector(_FakeConnector(inventory=_aws_inventory())):
        second = await reconcile_endpoint(db_session, endpoint)

    assert first.ok and second.ok
    assert second.blocks_created == 0
    assert second.subnets_created == 0
    assert second.addresses_created == 0

    blocks = (
        (await db_session.execute(select(IPBlock).where(IPBlock.cloud_endpoint_id == endpoint.id)))
        .scalars()
        .all()
    )
    subs = (
        (await db_session.execute(select(Subnet).where(Subnet.cloud_endpoint_id == endpoint.id)))
        .scalars()
        .all()
    )
    inst = (
        (
            await db_session.execute(
                select(IPAddress).where(
                    IPAddress.cloud_endpoint_id == endpoint.id,
                    IPAddress.status == "cloud-instance",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(blocks) == 1
    assert len(subs) == 2
    assert len(inst) == 2


@pytest.mark.asyncio
async def test_user_modified_row_not_overwritten(db_session: AsyncSession) -> None:
    """An operator edit (user_modified_at set) survives a re-sync."""
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    with _patch_connector(_FakeConnector(inventory=_aws_inventory())):
        await reconcile_endpoint(db_session, endpoint)

    row = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.cloud_endpoint_id == endpoint.id,
                IPAddress.address == "10.0.1.10",
            )
        )
    ).scalar_one()
    assert row.hostname == "web-1"

    # Operator renames the row (simulating the API write path).
    row.hostname = "golden-web"
    row.description = "Hand-curated — keep"
    row.user_modified_at = datetime.now(UTC)
    await db_session.commit()

    # Re-sync: cloud still reports "web-1" but the operator's edits hold.
    with _patch_connector(_FakeConnector(inventory=_aws_inventory())):
        await reconcile_endpoint(db_session, endpoint)
    await db_session.refresh(row)
    assert row.hostname == "golden-web"
    assert row.description == "Hand-curated — keep"


@pytest.mark.asyncio
async def test_removed_instance_prunes_its_row(db_session: AsyncSession) -> None:
    """An instance gone from the inventory has its mirrored row deleted."""
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    with _patch_connector(_FakeConnector(inventory=_aws_inventory())):
        await reconcile_endpoint(db_session, endpoint)
    assert (
        await db_session.scalar(select(IPAddress).where(IPAddress.address == "10.0.2.20"))
    ) is not None

    # Second pass: db-1 (10.0.2.20) is gone.
    inv = _aws_inventory()
    inv.instances = [inv.instances[0]]  # drop db-1
    with _patch_connector(_FakeConnector(inventory=inv)):
        summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok
    assert summary.addresses_deleted >= 1
    gone = await db_session.scalar(select(IPAddress).where(IPAddress.address == "10.0.2.20"))
    assert gone is None
    # web-1 still present.
    assert (
        await db_session.scalar(select(IPAddress).where(IPAddress.address == "10.0.1.10"))
    ) is not None


@pytest.mark.asyncio
async def test_partial_pull_suppresses_absence_delete(db_session: AsyncSession) -> None:
    """#430 — when a scope failed mid-fetch (failed_scopes non-empty), the
    reconciler upserts what it got but must NOT run the absence-delete pass.

    A region throttle that drops db-1 from the inventory would otherwise
    purge its mirrored row even though the row still exists upstream."""
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    # First (clean) pass mirrors both instances.
    with _patch_connector(_FakeConnector(inventory=_aws_inventory())):
        await reconcile_endpoint(db_session, endpoint)
    assert (
        await db_session.scalar(select(IPAddress).where(IPAddress.address == "10.0.2.20"))
    ) is not None

    # Second pass: db-1 (10.0.2.20) is missing, but ONLY because a region
    # failed mid-pull — the inventory is incomplete, not authoritatively empty.
    inv = _aws_inventory()
    inv.instances = [inv.instances[0]]  # db-1 absent from the partial inventory
    inv.failed_scopes = ["region us-east-1"]
    with _patch_connector(_FakeConnector(inventory=inv)):
        summary = await reconcile_endpoint(db_session, endpoint)

    # No deletes ran; the row the partial pull "lost" is preserved.
    assert summary.addresses_deleted == 0
    assert summary.subnets_deleted == 0
    assert (
        await db_session.scalar(select(IPAddress).where(IPAddress.address == "10.0.2.20"))
    ) is not None
    # The incompleteness is surfaced, not reported as a clean sync.
    assert endpoint.last_sync_error is not None
    assert "region us-east-1" in endpoint.last_sync_error


@pytest.mark.asyncio
async def test_public_ip_lands_when_enclosing_subnet_mirrored(
    db_session: AsyncSession,
) -> None:
    """When a public-range subnet is mirrored, public + LB IPs inside it
    materialise (rather than being skipped)."""
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    inv = CloudInventory(
        account_id="123456789012",
        networks=[CloudNetwork(id="vpc-1", name="edge-vpc", cidrs=("198.51.100.0/24",))],
        subnets=[
            CloudSubnet(id="s-pub", name="public", network_id="vpc-1", cidr="198.51.100.0/24")
        ],
        public_ips=[CloudPublicIP(address="198.51.100.50", name="eip-edge", attached=True)],
        load_balancers=[
            CloudLoadBalancer(id="lb-1", name="edge-lb", frontend_ips=("198.51.100.60",))
        ],
    )
    with _patch_connector(_FakeConnector(inventory=inv)):
        summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok, summary.error
    pub = await db_session.scalar(select(IPAddress).where(IPAddress.address == "198.51.100.50"))
    lb = await db_session.scalar(select(IPAddress).where(IPAddress.address == "198.51.100.60"))
    assert pub is not None and pub.status == "cloud-public"
    assert lb is not None and lb.status == "cloud-lb"


@pytest.mark.asyncio
async def test_mirror_load_balancers_off_skips_lb_rows(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space, mirror_load_balancers=False)
    await db_session.commit()

    inv = CloudInventory(
        account_id="123456789012",
        networks=[CloudNetwork(id="vpc-1", name="edge-vpc", cidrs=("198.51.100.0/24",))],
        subnets=[
            CloudSubnet(id="s-pub", name="public", network_id="vpc-1", cidr="198.51.100.0/24")
        ],
        load_balancers=[
            CloudLoadBalancer(id="lb-1", name="edge-lb", frontend_ips=("198.51.100.60",))
        ],
    )
    fake = _FakeConnector(inventory=inv)
    with _patch_connector(fake):
        summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok
    lb = await db_session.scalar(select(IPAddress).where(IPAddress.status == "cloud-lb"))
    assert lb is None
    # mirror_load_balancers=False flows through to the connector fetch call.
    assert fake.fetch_calls == [{"include_stopped": False, "include_load_balancers": False}]


@pytest.mark.asyncio
async def test_operator_subnet_reused_not_duplicated(
    db_session: AsyncSession,
) -> None:
    """A pre-existing operator subnet at the exact CIDR is reused; the
    instance IP still mirrors into it."""
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    parent = IPBlock(space_id=space.id, network="10.0.0.0/8", name="corp")
    db_session.add(parent)
    await db_session.flush()
    operator_sub = Subnet(
        space_id=space.id,
        block_id=parent.id,
        network="10.0.1.0/24",
        name="hand-built",
        description="curated",
        total_ips=256,
    )
    db_session.add(operator_sub)
    await db_session.commit()

    inv = CloudInventory(
        account_id="123456789012",
        networks=[CloudNetwork(id="vpc-1", name="prod-vpc", cidrs=("10.0.0.0/16",))],
        subnets=[CloudSubnet(id="s-a", name="app-a", network_id="vpc-1", cidr="10.0.1.0/24")],
        instances=[
            CloudInstance(
                id="i-aaa",
                name="web-1",
                running=True,
                nics=(CloudNic(private_ip="10.0.1.10"),),
            )
        ],
    )
    with _patch_connector(_FakeConnector(inventory=inv)):
        summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok, summary.error
    assert summary.subnets_matched == 1

    rows = (
        (
            await db_session.execute(
                select(Subnet).where(Subnet.space_id == space.id, Subnet.network == "10.0.1.0/24")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    only = rows[0]
    assert only.id == operator_sub.id
    assert only.cloud_endpoint_id is None  # ownership not claimed
    assert only.name == "hand-built"

    # Instance IP lands in the reused operator subnet.
    ip = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.cloud_endpoint_id == endpoint.id,
                IPAddress.status == "cloud-instance",
            )
        )
    ).scalar_one()
    assert ip.subnet_id == operator_sub.id


@pytest.mark.asyncio
async def test_endpoint_delete_cascades(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    with _patch_connector(_FakeConnector(inventory=_aws_inventory())):
        await reconcile_endpoint(db_session, endpoint)

    endpoint_id = endpoint.id
    await db_session.delete(endpoint)
    await db_session.commit()

    subs = (
        (await db_session.execute(select(Subnet).where(Subnet.cloud_endpoint_id == endpoint_id)))
        .scalars()
        .all()
    )
    addrs = (
        (
            await db_session.execute(
                select(IPAddress).where(IPAddress.cloud_endpoint_id == endpoint_id)
            )
        )
        .scalars()
        .all()
    )
    assert list(subs) == []
    assert list(addrs) == []


# ── Error paths ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_credentials_sets_last_sync_error(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    endpoint = CloudEndpoint(
        name=f"cloud-{uuid.uuid4().hex[:6]}",
        provider="aws",
        credentials_encrypted=b"",  # unset
        provider_config={},
        regions=[],
        ipam_space_id=space.id,
    )
    db_session.add(endpoint)
    await db_session.commit()

    summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok is False
    assert summary.error is not None
    await db_session.refresh(endpoint)
    assert endpoint.last_sync_error is not None
    assert endpoint.last_synced_at is not None


@pytest.mark.asyncio
async def test_connector_error_sets_last_sync_error(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    endpoint = await _make_endpoint(db_session, space)
    await db_session.commit()

    fake = _FakeConnector(error=CloudConnectorError("auth failed: invalid keys"))
    with _patch_connector(fake):
        summary = await reconcile_endpoint(db_session, endpoint)

    assert summary.ok is False
    assert summary.error is not None
    assert "auth failed" in summary.error
    await db_session.refresh(endpoint)
    assert endpoint.last_sync_error is not None
    assert "auth failed" in (endpoint.last_sync_error or "")
    assert endpoint.last_synced_at is not None
