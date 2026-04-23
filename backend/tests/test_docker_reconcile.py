"""Tests for the Docker reconciler.

Stub the Docker client since we don't want a real daemon. Same
structure as ``test_kubernetes_reconcile`` — validates smart parent-
block detection, network → subnet creation, container mirroring
(opt-in), default-network filtering, and cascade-delete on host
removal.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.docker import DockerHost
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.docker.client import _DockerContainer, _DockerNetwork
from app.services.docker.reconcile import reconcile_host

# ── Fixtures ─────────────────────────────────────────────────────────


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"docker-test-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_block(db: AsyncSession, space: IPSpace, network: str) -> IPBlock:
    block = IPBlock(
        space_id=space.id,
        network=network,
        name=f"blk-{uuid.uuid4().hex[:6]}",
    )
    db.add(block)
    await db.flush()
    return block


async def _make_host(
    db: AsyncSession,
    space: IPSpace,
    *,
    mirror_containers: bool = False,
    include_default_networks: bool = False,
    include_stopped: bool = False,
) -> DockerHost:
    host = DockerHost(
        name=f"docker-{uuid.uuid4().hex[:6]}",
        connection_type="tcp",
        endpoint="docker.example.test:2376",
        ipam_space_id=space.id,
        mirror_containers=mirror_containers,
        include_default_networks=include_default_networks,
        include_stopped_containers=include_stopped,
        client_key_encrypted=b"",
    )
    db.add(host)
    await db.flush()
    return host


class _FakeClient:
    def __init__(
        self,
        *,
        networks: list[_DockerNetwork] | None = None,
        containers: list[_DockerContainer] | None = None,
    ) -> None:
        self.networks = networks or []
        self.containers = containers or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_networks(self):
        return self.networks

    async def list_containers(self, *, include_stopped: bool):
        # include_stopped filter is applied by the reconciler via
        # host.include_stopped_containers on the status-check side,
        # so for the fake client we just return everything and trust
        # the reconciler.
        del include_stopped
        return self.containers


def _patch_client(fake: _FakeClient):
    def _ctor(**_kwargs):
        return fake

    return patch(
        "app.services.docker.reconcile.DockerClient",
        side_effect=_ctor,
    )


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_network_without_parent_creates_wrapper_and_subnet(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    host = await _make_host(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="backend",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    assert summary.ok, summary.error
    assert summary.blocks_created == 1
    assert summary.subnets_created == 1

    res = await db_session.execute(select(Subnet).where(Subnet.docker_host_id == host.id))
    sub = res.scalar_one()
    assert str(sub.network) == "172.20.0.0/16"
    assert str(sub.gateway) == "172.20.0.1"
    # Docker subnets use normal LAN semantics — no kubernetes_semantics flag.
    assert sub.kubernetes_semantics is False


@pytest.mark.asyncio
async def test_enclosing_operator_block_skips_wrapper(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    parent = await _make_block(db_session, space, "172.16.0.0/12")
    host = await _make_host(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="backend",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    assert summary.ok
    assert summary.blocks_created == 0

    res = await db_session.execute(select(Subnet).where(Subnet.docker_host_id == host.id))
    sub = res.scalar_one()
    assert sub.block_id == parent.id


@pytest.mark.asyncio
async def test_rfc1918_supernet_auto_created(db_session: AsyncSession) -> None:
    """If no enclosing block exists, the reconciler should create an
    RFC 1918 parent block (unowned) so the docker network nests under
    it rather than creating its own top-level wrapper.
    """
    space = await _make_space(db_session)
    host = await _make_host(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="myapp_default",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    assert summary.ok, summary.error

    # A 172.16.0.0/12 parent block exists and is unowned.
    res = await db_session.execute(
        select(IPBlock).where(IPBlock.space_id == space.id, IPBlock.network == "172.16.0.0/12")
    )
    parent = res.scalar_one()
    assert parent.docker_host_id is None
    # No host-owned wrapper at /16 — the subnet nests directly under /12.
    res = await db_session.execute(select(IPBlock).where(IPBlock.docker_host_id == host.id))
    assert list(res.scalars().all()) == []
    # Subnet parent is the /12.
    res = await db_session.execute(select(Subnet).where(Subnet.docker_host_id == host.id))
    sub = res.scalar_one()
    assert sub.block_id == parent.id


@pytest.mark.asyncio
async def test_cgnat_supernet_auto_created(db_session: AsyncSession) -> None:
    """RFC 6598 (100.64/10) should get the same supernet treatment
    even though it isn't strictly RFC 1918 — it shows up in some
    k3s / Tailscale-adjacent clusters.
    """
    space = await _make_space(db_session)
    host = await _make_host(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="tailscale",
                driver="bridge",
                scope="local",
                subnets=[("100.80.0.0/16", "100.80.0.1")],
            )
        ]
    )
    with _patch_client(fake):
        await reconcile_host(db_session, host)

    res = await db_session.execute(
        select(IPBlock).where(IPBlock.space_id == space.id, IPBlock.network == "100.64.0.0/10")
    )
    assert res.scalar_one() is not None


@pytest.mark.asyncio
async def test_default_networks_skipped_by_default(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    host = await _make_host(db_session, space)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="default-bridge",
                name="bridge",
                driver="bridge",
                scope="local",
                subnets=[("172.17.0.0/16", "172.17.0.1")],
            ),
            _DockerNetwork(
                id="net1",
                name="myapp_default",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            ),
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    assert summary.subnets_created == 1  # only myapp_default


@pytest.mark.asyncio
async def test_default_networks_included_when_opted_in(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    host = await _make_host(db_session, space, include_default_networks=True)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="default-bridge",
                name="bridge",
                driver="bridge",
                scope="local",
                subnets=[("172.17.0.0/16", "172.17.0.1")],
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    assert summary.subnets_created == 1


@pytest.mark.asyncio
async def test_swarm_overlay_networks_always_skipped(
    db_session: AsyncSession,
) -> None:
    """Overlay networks are cluster-wide; mirroring them per host would
    create duplicate IPAM entries on every swarm node."""
    space = await _make_space(db_session)
    host = await _make_host(db_session, space, include_default_networks=True)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="overlay1",
                name="my-overlay",
                driver="overlay",
                scope="swarm",
                subnets=[("10.0.0.0/24", "10.0.0.1")],
            )
        ]
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    assert summary.subnets_created == 0


@pytest.mark.asyncio
async def test_containers_not_mirrored_by_default(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    host = await _make_host(db_session, space, mirror_containers=False)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="backend",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            )
        ],
        containers=[
            _DockerContainer(
                id="c1",
                name="web",
                image="nginx:latest",
                state="running",
                status="Up 1 hour",
                ip_bindings=[("backend", "172.20.0.5")],
            )
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    # Only the gateway address lands (from the subnet). The
    # container IP does not.
    res = await db_session.execute(
        select(IPAddress).where(
            IPAddress.docker_host_id == host.id,
            IPAddress.status == "docker-container",
        )
    )
    assert list(res.scalars().all()) == []
    assert summary.addresses_created >= 1  # at least the gateway


@pytest.mark.asyncio
async def test_containers_mirrored_when_opted_in(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    host = await _make_host(db_session, space, mirror_containers=True)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="backend",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            )
        ],
        containers=[
            _DockerContainer(
                id="c1",
                name="web",
                image="nginx:latest",
                state="running",
                status="Up 1 hour",
                ip_bindings=[("backend", "172.20.0.5")],
                compose_project="myapp",
                compose_service="web",
            )
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    assert summary.ok
    res = await db_session.execute(
        select(IPAddress).where(
            IPAddress.docker_host_id == host.id,
            IPAddress.status == "docker-container",
        )
    )
    row = res.scalar_one()
    assert str(row.address) == "172.20.0.5"
    # Compose labels give us the project/service hostname form.
    assert row.hostname == "myapp.web"


@pytest.mark.asyncio
async def test_stopped_containers_skipped_by_default(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    host = await _make_host(db_session, space, mirror_containers=True)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="backend",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            )
        ],
        containers=[
            _DockerContainer(
                id="c1",
                name="old",
                image="x",
                state="exited",
                status="Exited",
                ip_bindings=[("backend", "172.20.0.10")],
            ),
            _DockerContainer(
                id="c2",
                name="new",
                image="x",
                state="running",
                status="Up",
                ip_bindings=[("backend", "172.20.0.11")],
            ),
        ],
    )
    with _patch_client(fake):
        summary = await reconcile_host(db_session, host)

    res = await db_session.execute(
        select(IPAddress).where(
            IPAddress.docker_host_id == host.id,
            IPAddress.status == "docker-container",
        )
    )
    rows = list(res.scalars().all())
    assert [str(r.address) for r in rows] == ["172.20.0.11"]
    assert summary.container_count == 2  # reported, just filtered by state


@pytest.mark.asyncio
async def test_host_delete_cascades(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    host = await _make_host(db_session, space, mirror_containers=True)
    await db_session.commit()

    fake = _FakeClient(
        networks=[
            _DockerNetwork(
                id="net1",
                name="backend",
                driver="bridge",
                scope="local",
                subnets=[("172.20.0.0/16", "172.20.0.1")],
            )
        ],
        containers=[
            _DockerContainer(
                id="c1",
                name="web",
                image="x",
                state="running",
                status="Up",
                ip_bindings=[("backend", "172.20.0.5")],
            )
        ],
    )
    with _patch_client(fake):
        await reconcile_host(db_session, host)

    # Subnet + addresses carry the host FK and should exist. Blocks
    # don't — the /12 supernet is unowned so it's not tied to the host.
    for model, minimum in ((Subnet, 1), (IPAddress, 1)):
        res = await db_session.execute(select(model).where(model.docker_host_id == host.id))
        assert len(list(res.scalars().all())) >= minimum, model.__name__

    host_id = host.id
    await db_session.delete(host)
    await db_session.commit()

    for model in (IPBlock, Subnet, IPAddress):
        res = await db_session.execute(select(model).where(model.docker_host_id == host_id))
        assert list(res.scalars().all()) == [], f"{model.__name__} orphaned"

    # Supernet is unowned and survives host removal.
    res = await db_session.execute(select(IPBlock).where(IPBlock.network == "172.16.0.0/12"))
    assert res.scalar_one() is not None
