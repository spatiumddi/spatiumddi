"""Tests for the Kubernetes reconciler — Phase 1b.

We stub the ``KubernetesClient`` since we don't want the test DB to
actually reach out to a real cluster. The reconciler is the interesting
behavior: smart parent-block detection, auto-subnet creation with
``kubernetes_semantics`` set, ClusterIP + LB + node + (optional) pod
mirroring, ingress → DNS, cascade-delete on cluster removal.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.kubernetes import KubernetesCluster
from app.services.kubernetes.client import (
    _K8sIngress,
    _K8sLBService,
    _K8sNode,
    _K8sPod,
    _K8sService,
)
from app.services.kubernetes.reconcile import reconcile_cluster

# ── Fixtures ─────────────────────────────────────────────────────────


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"k8s-test-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_block(db: AsyncSession, space: IPSpace, network: str) -> IPBlock:
    block = IPBlock(space_id=space.id, network=network, name=f"blk-{uuid.uuid4().hex[:6]}")
    db.add(block)
    await db.flush()
    return block


async def _make_subnet(
    db: AsyncSession, space: IPSpace, network: str, *, block: IPBlock | None = None
) -> Subnet:
    if block is None:
        block = await _make_block(db, space, "0.0.0.0/0")
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=network,
        name=f"sub-{uuid.uuid4().hex[:6]}",
    )
    db.add(subnet)
    await db.flush()
    return subnet


async def _make_cluster(
    db: AsyncSession,
    space: IPSpace,
    *,
    pod_cidr: str = "10.244.0.0/16",
    service_cidr: str = "10.96.0.0/12",
    dns_group_id: uuid.UUID | None = None,
    mirror_pods: bool = False,
) -> KubernetesCluster:
    cluster = KubernetesCluster(
        name=f"cluster-{uuid.uuid4().hex[:6]}",
        api_server_url="https://k8s.example.test:6443",
        ca_bundle_pem="",
        token_encrypted=encrypt_str("faketoken"),
        ipam_space_id=space.id,
        dns_group_id=dns_group_id,
        pod_cidr=pod_cidr,
        service_cidr=service_cidr,
        mirror_pods=mirror_pods,
    )
    db.add(cluster)
    await db.flush()
    return cluster


class _FakeClient:
    """Mimics ``KubernetesClient`` async-context behavior + list methods."""

    def __init__(
        self,
        *,
        nodes: list[_K8sNode] | None = None,
        lb_services: list[_K8sLBService] | None = None,
        services: list[_K8sService] | None = None,
        pods: list[_K8sPod] | None = None,
        ingresses: list[_K8sIngress] | None = None,
    ) -> None:
        self.nodes = nodes or []
        self.lb_services = lb_services or []
        self.services = services or []
        self.pods = pods or []
        self.ingresses = ingresses or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_nodes(self):
        return self.nodes

    async def list_loadbalancer_services(self):
        return self.lb_services

    async def list_services(self):
        return self.services

    async def list_pods(self):
        return self.pods

    async def list_ingresses(self):
        return self.ingresses


def _patch_client(fake: _FakeClient):
    def _ctor(**_kwargs):
        return fake

    return patch(
        "app.services.kubernetes.reconcile.KubernetesClient",
        side_effect=_ctor,
    )


# ── Block + subnet auto-creation ────────────────────────────────────


@pytest.mark.asyncio
async def test_rfc1918_supernet_auto_created(db_session: AsyncSession) -> None:
    """No enclosing block exists → reconciler creates a private-
    address parent (10.0.0.0/8) and nests both the pod and service
    CIDR subnets directly under it. No cluster-owned wrappers.
    """
    space = await _make_space(db_session)
    cluster = await _make_cluster(
        db_session, space, pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12"
    )
    await db_session.commit()

    with _patch_client(_FakeClient()):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.ok, summary.error
    # One supernet /8 created; no cluster-owned wrappers at /16.
    assert summary.blocks_created == 1
    assert summary.subnets_created == 2

    res = await db_session.execute(
        select(IPBlock).where(IPBlock.space_id == space.id, IPBlock.network == "10.0.0.0/8")
    )
    parent = res.scalar_one()
    # Supernet is unowned so it survives cluster removal.
    assert parent.kubernetes_cluster_id is None

    res = await db_session.execute(
        select(IPBlock).where(IPBlock.kubernetes_cluster_id == cluster.id)
    )
    assert list(res.scalars().all()) == []

    res = await db_session.execute(select(Subnet).where(Subnet.kubernetes_cluster_id == cluster.id))
    subnets = list(res.scalars().all())
    assert sorted(str(s.network) for s in subnets) == ["10.244.0.0/16", "10.96.0.0/12"]
    assert all(s.block_id == parent.id for s in subnets)
    assert all(s.kubernetes_semantics for s in subnets)


@pytest.mark.asyncio
async def test_enclosing_operator_block_skips_wrapper_creation(
    db_session: AsyncSession,
) -> None:
    """Operator has a broad block (10.0.0.0/8) covering the pod + service
    CIDRs → reconciler nests the auto-subnets under it and does NOT
    create its own wrapper blocks.
    """
    space = await _make_space(db_session)
    parent = await _make_block(db_session, space, "10.0.0.0/8")
    cluster = await _make_cluster(
        db_session, space, pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12"
    )
    await db_session.commit()

    with _patch_client(_FakeClient()):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.ok, summary.error
    assert summary.blocks_created == 0
    assert summary.subnets_created == 2

    res = await db_session.execute(
        select(IPBlock).where(IPBlock.kubernetes_cluster_id == cluster.id)
    )
    assert list(res.scalars().all()) == []  # no wrapper blocks

    res = await db_session.execute(select(Subnet).where(Subnet.kubernetes_cluster_id == cluster.id))
    for sub in res.scalars().all():
        assert sub.block_id == parent.id, "subnet should nest under the operator block"


@pytest.mark.asyncio
async def test_cidr_removed_deletes_subnet_and_wrapper(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    cluster = await _make_cluster(
        db_session, space, pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12"
    )
    await db_session.commit()
    with _patch_client(_FakeClient()):
        await reconcile_cluster(db_session, cluster)

    # Operator drops the service_cidr on the cluster config. Only the
    # service CIDR subnet disappears; the 10.0.0.0/8 supernet stays
    # (unowned, still backing the pod CIDR).
    cluster.service_cidr = ""
    await db_session.commit()
    with _patch_client(_FakeClient()):
        summary = await reconcile_cluster(db_session, cluster)
    assert summary.subnets_deleted == 1
    assert summary.blocks_deleted == 0

    res = await db_session.execute(select(Subnet).where(Subnet.kubernetes_cluster_id == cluster.id))
    assert [str(s.network) for s in res.scalars().all()] == ["10.244.0.0/16"]
    # Supernet still exists and is unowned.
    res = await db_session.execute(
        select(IPBlock).where(IPBlock.space_id == space.id, IPBlock.network == "10.0.0.0/8")
    )
    assert res.scalar_one().kubernetes_cluster_id is None


# ── Node address reconciliation ─────────────────────────────────────


@pytest.mark.asyncio
async def test_nodes_create_kubernetes_node_addresses(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space, "10.0.0.0/24")
    cluster = await _make_cluster(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        nodes=[
            _K8sNode(name="node-a", internal_ip="10.0.0.5", ready=True),
            _K8sNode(name="node-b", internal_ip="10.0.0.6", ready=True),
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.ok
    assert summary.addresses_created == 2
    res = await db_session.execute(
        select(IPAddress).where(IPAddress.kubernetes_cluster_id == cluster.id)
    )
    rows = sorted(res.scalars().all(), key=lambda r: str(r.address))
    assert [str(r.address) for r in rows] == ["10.0.0.5", "10.0.0.6"]
    assert all(r.status == "kubernetes-node" for r in rows)
    assert all(r.subnet_id == subnet.id for r in rows)


@pytest.mark.asyncio
async def test_node_without_matching_subnet_is_skipped(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    await _make_subnet(db_session, space, "10.0.0.0/24")
    cluster = await _make_cluster(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        nodes=[
            _K8sNode(name="node-far", internal_ip="192.168.99.5", ready=True),
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.ok
    assert summary.addresses_created == 0
    assert summary.skipped_no_subnet == 1


@pytest.mark.asyncio
async def test_removed_node_deletes_address(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    await _make_subnet(db_session, space, "10.0.0.0/24")
    cluster = await _make_cluster(db_session, space)
    await db_session.commit()

    with _patch_client(
        _FakeClient(nodes=[_K8sNode(name="n1", internal_ip="10.0.0.5", ready=True)])
    ):
        await reconcile_cluster(db_session, cluster)

    with _patch_client(_FakeClient()):
        summary = await reconcile_cluster(db_session, cluster)
    assert summary.addresses_deleted == 1

    res = await db_session.execute(
        select(IPAddress).where(IPAddress.kubernetes_cluster_id == cluster.id)
    )
    assert list(res.scalars().all()) == []


# ── LoadBalancer + ClusterIP + Pod mirroring ────────────────────────


@pytest.mark.asyncio
async def test_lb_service_creates_kubernetes_lb_address(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    await _make_subnet(db_session, space, "10.50.0.0/24")
    cluster = await _make_cluster(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        lb_services=[
            _K8sLBService(
                namespace="prod",
                name="api-gateway",
                ip="10.50.0.42",
                hostname=None,
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.addresses_created == 1
    res = await db_session.execute(
        select(IPAddress).where(
            IPAddress.kubernetes_cluster_id == cluster.id,
            IPAddress.status == "kubernetes-lb",
        )
    )
    row = res.scalar_one()
    assert str(row.address) == "10.50.0.42"
    assert row.hostname == "api-gateway.prod"


@pytest.mark.asyncio
async def test_cluster_ip_service_lands_in_auto_subnet(
    db_session: AsyncSession,
) -> None:
    """Service ClusterIPs land in the auto-created service-CIDR subnet.
    This exercises the end-to-end flow: reconciler creates the
    10.96.0.0/12 subnet, then places the ClusterIP inside it.
    """
    space = await _make_space(db_session)
    cluster = await _make_cluster(db_session, space, pod_cidr="", service_cidr="10.96.0.0/12")
    await db_session.commit()

    fake = _FakeClient(
        services=[
            _K8sService(
                namespace="default",
                name="kubernetes",
                cluster_ip="10.96.0.1",
                service_type="ClusterIP",
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.subnets_created == 1
    assert summary.addresses_created == 1
    res = await db_session.execute(
        select(IPAddress).where(IPAddress.kubernetes_cluster_id == cluster.id)
    )
    row = res.scalar_one()
    assert str(row.address) == "10.96.0.1"
    assert row.status == "kubernetes-service"
    assert row.hostname == "kubernetes.default"


@pytest.mark.asyncio
async def test_pods_not_mirrored_by_default(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    cluster = await _make_cluster(db_session, space, pod_cidr="10.244.0.0/16", service_cidr="")
    await db_session.commit()

    fake = _FakeClient(
        pods=[
            _K8sPod(namespace="default", name="pod-a", pod_ip="10.244.1.5", phase="Running"),
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    # mirror_pods defaults to False — reconciler doesn't even call
    # list_pods, so no pod addresses land regardless of what we return.
    assert summary.addresses_created == 0


@pytest.mark.asyncio
async def test_pods_mirrored_when_opt_in(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    cluster = await _make_cluster(
        db_session,
        space,
        pod_cidr="10.244.0.0/16",
        service_cidr="",
        mirror_pods=True,
    )
    await db_session.commit()

    fake = _FakeClient(
        pods=[
            _K8sPod(namespace="default", name="pod-a", pod_ip="10.244.1.5", phase="Running"),
            _K8sPod(
                namespace="kube-system", name="coredns-x", pod_ip="10.244.2.3", phase="Running"
            ),
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.addresses_created == 2
    res = await db_session.execute(
        select(IPAddress).where(IPAddress.kubernetes_cluster_id == cluster.id)
    )
    rows = sorted(res.scalars().all(), key=lambda r: str(r.address))
    assert [r.status for r in rows] == ["kubernetes-pod", "kubernetes-pod"]


# ── Ingress → DNS reconciliation ────────────────────────────────────


async def _make_dns_group_with_zone(
    db: AsyncSession, zone_name: str = "apps.example.com."
) -> tuple[DNSServerGroup, DNSZone]:
    group = DNSServerGroup(name=f"grp-{uuid.uuid4().hex[:6]}", description="")
    db.add(group)
    await db.flush()
    zone = DNSZone(
        group_id=group.id,
        name=zone_name,
        zone_type="primary",
    )
    db.add(zone)
    await db.flush()
    return group, zone


@pytest.mark.asyncio
async def test_ingress_creates_a_record(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    group, zone = await _make_dns_group_with_zone(db_session, "apps.example.com.")
    cluster = await _make_cluster(db_session, space, dns_group_id=group.id)
    await db_session.commit()

    fake = _FakeClient(
        ingresses=[
            _K8sIngress(
                namespace="prod",
                name="api",
                hosts=["api.apps.example.com"],
                target_ip="10.50.0.42",
                target_hostname=None,
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.records_created == 1
    res = await db_session.execute(
        select(DNSRecord).where(DNSRecord.kubernetes_cluster_id == cluster.id)
    )
    rec = res.scalar_one()
    assert rec.zone_id == zone.id
    assert rec.name == "api"
    assert rec.record_type == "A"
    assert rec.value == "10.50.0.42"
    assert rec.auto_generated is True


@pytest.mark.asyncio
async def test_ingress_hostname_creates_cname(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    group, _ = await _make_dns_group_with_zone(db_session, "apps.example.com.")
    cluster = await _make_cluster(db_session, space, dns_group_id=group.id)
    await db_session.commit()

    fake = _FakeClient(
        ingresses=[
            _K8sIngress(
                namespace="prod",
                name="api",
                hosts=["api.apps.example.com"],
                target_ip=None,
                target_hostname="aws-lb-123.elb.amazonaws.com",
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.records_created == 1
    res = await db_session.execute(
        select(DNSRecord).where(DNSRecord.kubernetes_cluster_id == cluster.id)
    )
    rec = res.scalar_one()
    assert rec.record_type == "CNAME"
    assert rec.value == "aws-lb-123.elb.amazonaws.com."


@pytest.mark.asyncio
async def test_ingress_without_matching_zone_is_skipped(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    group, _ = await _make_dns_group_with_zone(db_session, "apps.example.com.")
    cluster = await _make_cluster(db_session, space, dns_group_id=group.id)
    await db_session.commit()

    fake = _FakeClient(
        ingresses=[
            _K8sIngress(
                namespace="prod",
                name="misplaced",
                hosts=["foo.unrelated-zone.tld"],
                target_ip="10.50.0.42",
                target_hostname=None,
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_cluster(db_session, cluster)

    assert summary.records_created == 0
    assert summary.skipped_no_zone == 1


# ── Cascade delete ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cluster_delete_cascades_rows(db_session: AsyncSession) -> None:
    """Removing the cluster row must sweep every mirrored row (blocks,
    subnets, addresses, records) via FK ON DELETE CASCADE."""
    space = await _make_space(db_session)
    # No pre-existing enclosing block — reconciler will create wrappers
    # so we exercise the full cascade.
    group, _ = await _make_dns_group_with_zone(db_session, "apps.example.com.")
    cluster = await _make_cluster(
        db_session,
        space,
        pod_cidr="10.244.0.0/16",
        service_cidr="10.96.0.0/12",
        dns_group_id=group.id,
    )
    await db_session.commit()

    with _patch_client(
        _FakeClient(
            nodes=[_K8sNode(name="n", internal_ip="10.244.0.5", ready=True)],
            ingresses=[
                _K8sIngress(
                    namespace="d",
                    name="i",
                    hosts=["x.apps.example.com"],
                    target_ip="10.244.0.5",
                    target_hostname=None,
                )
            ],
        )
    ):
        await reconcile_cluster(db_session, cluster)

    # Sanity: two cluster-owned subnets + one record mirrored. The
    # 10.0.0.0/8 private-address parent is unowned (no FK), so it
    # isn't counted on the cluster-owned side.
    for model, expected in ((Subnet, 2), (DNSRecord, 1)):
        res = await db_session.execute(
            select(model).where(model.kubernetes_cluster_id == cluster.id)
        )
        assert len(list(res.scalars().all())) == expected, model.__name__

    # Confirm the supernet is present but unowned — it should not
    # cascade when we delete the cluster.
    res = await db_session.execute(
        select(IPBlock).where(IPBlock.space_id == space.id, IPBlock.network == "10.0.0.0/8")
    )
    supernet = res.scalar_one()
    assert supernet.kubernetes_cluster_id is None

    cluster_id = cluster.id
    supernet_id = supernet.id
    await db_session.delete(cluster)
    await db_session.commit()

    for model in (IPAddress, IPBlock, Subnet, DNSRecord):
        res = await db_session.execute(
            select(model).where(model.kubernetes_cluster_id == cluster_id)
        )
        assert list(res.scalars().all()) == [], f"{model.__name__} orphaned"

    # Supernet should still be here — it's unowned, not linked to the
    # cluster via FK, so cluster delete leaves it intact.
    assert await db_session.get(IPBlock, supernet_id) is not None
