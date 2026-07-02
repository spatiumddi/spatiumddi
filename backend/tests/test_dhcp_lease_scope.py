"""Subnet-scoping of the DHCP lease-removal → IPAM-mirror revoke paths.

SpatiumDDI supports overlapping private ranges across IPSpaces/VRFs, so
the same address (e.g. 10.0.0.50) can legitimately be an
``auto_from_lease`` IPAM mirror in more than one subnet. Both
lease-removal paths used to locate the mirror by address alone, which:

  * crashed ``pull_leases`` with ``MultipleResultsFound`` (the lookup
    ended in ``scalar_one_or_none()``), and
  * made ``dhcp_lease_cleanup`` delete *every* same-address mirror across
    all subnets (it did ``for row in .scalars().all(): delete(row)``).

These tests stand up two same-address mirrors in different IPSpaces and
prove only the lease's own-subnet mirror is touched. Run against a real
Postgres so the INET / MACADDR / CIDR column types behave authentically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPLease, DHCPScope, DHCPServer, DHCPServerGroup
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

SHARED_IP = "10.0.0.50"


async def _make_subnet_with_mirror(
    db: AsyncSession,
    *,
    block_cidr: str,
    subnet_cidr: str,
    ip: str,
) -> tuple[Subnet, IPAddress]:
    """Create an IPSpace → IPBlock → Subnet with one auto_from_lease IPAddress."""
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=block_cidr, name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=subnet_cidr, name="sn")
    db.add(subnet)
    await db.flush()
    mirror = IPAddress(
        subnet_id=subnet.id,
        address=ip,
        status="dhcp",
        mac_address="aa:bb:cc:dd:ee:01",
        auto_from_lease=True,
    )
    db.add(mirror)
    await db.flush()
    return subnet, mirror


async def _make_server(db: AsyncSession) -> tuple[DHCPServerGroup, DHCPServer]:
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="windows_dhcp",  # the registered agentless driver
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    return grp, srv


class _StubDriver:
    """Minimal driver stub for pull_leases — only get_leases is used."""

    def __init__(self, leases: list[dict]) -> None:
        self._leases = leases

    async def get_leases(self, _server: DHCPServer) -> list[dict]:
        return self._leases


def _patch_pull_leases(monkeypatch: pytest.MonkeyPatch, leases: list[dict]) -> None:
    from app.services.dhcp import pull_leases as pl

    monkeypatch.setattr(pl, "get_driver", lambda _drv: _StubDriver(leases))
    monkeypatch.setattr(pl, "is_agentless", lambda _drv: True)
    import app.services.dns.ddns as ddns

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ddns, "apply_ddns_for_lease", _noop)
    monkeypatch.setattr(ddns, "revoke_ddns_for_lease", _noop)


# ── pull_leases absence-delete ───────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_leases_revoke_scoped_to_lease_subnet(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two same-address mirrors in different subnets; a stale lease in
    subnet A must revoke ONLY A's mirror — never B's — and must not crash."""
    from app.services.dhcp import pull_leases as pl

    grp, srv = await _make_server(db_session)
    subnet_a, mirror_a = await _make_subnet_with_mirror(
        db_session, block_cidr="10.0.0.0/16", subnet_cidr="10.0.0.0/24", ip=SHARED_IP
    )
    subnet_b, mirror_b = await _make_subnet_with_mirror(
        db_session, block_cidr="172.16.0.0/16", subnet_cidr="172.16.5.0/24", ip=SHARED_IP
    )
    # Scope tied to subnet A; lease backlinks to it.
    scope_a = DHCPScope(group_id=grp.id, subnet_id=subnet_a.id, is_active=True)
    db_session.add(scope_a)
    await db_session.flush()
    lease = DHCPLease(
        server_id=srv.id,
        scope_id=scope_a.id,
        ip_address=SHARED_IP,
        mac_address="aa:bb:cc:dd:ee:01",
        state="active",
    )
    db_session.add(lease)
    await db_session.commit()

    # Non-empty wire that EXCLUDES SHARED_IP → the lease is absent and gets
    # absence-deleted. The zero-wire floor guard (#482) skips the sweep only
    # when the ENTIRE wire is empty, so [] would no longer delete anything.
    _patch_pull_leases(
        monkeypatch, [{"ip_address": "203.0.113.9", "mac_address": "aa:bb:cc:dd:ee:fd"}]
    )
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.ipam_revoked == 1
    # A's mirror gone, B's mirror untouched.
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror_a.id))
    ).scalar_one_or_none() is None
    survivor = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror_b.id))
    ).scalar_one_or_none()
    assert survivor is not None
    assert str(survivor.address) == SHARED_IP
    assert survivor.subnet_id == subnet_b.id


@pytest.mark.asyncio
async def test_pull_leases_revoke_scoped_via_prefix_when_no_scope(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the stale lease has no scope FK, fall back to longest-prefix
    match so the correct subnet's mirror is the only one revoked."""
    from app.services.dhcp import pull_leases as pl

    _, srv = await _make_server(db_session)
    subnet_a, mirror_a = await _make_subnet_with_mirror(
        db_session, block_cidr="10.0.0.0/16", subnet_cidr="10.0.0.0/24", ip=SHARED_IP
    )
    _, mirror_b = await _make_subnet_with_mirror(
        db_session, block_cidr="172.16.0.0/16", subnet_cidr="172.16.5.0/24", ip=SHARED_IP
    )
    lease = DHCPLease(
        server_id=srv.id,
        scope_id=None,  # no backlink — exercise the prefix fallback
        ip_address=SHARED_IP,
        mac_address="aa:bb:cc:dd:ee:01",
        state="active",
    )
    db_session.add(lease)
    await db_session.commit()

    # Non-empty wire excluding SHARED_IP so absence-delete still fires under
    # the #482 zero-wire floor guard.
    _patch_pull_leases(
        monkeypatch, [{"ip_address": "203.0.113.9", "mac_address": "aa:bb:cc:dd:ee:fd"}]
    )
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.ipam_revoked == 1
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror_a.id))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror_b.id))
    ).scalar_one_or_none() is not None


# ── dhcp_lease_cleanup time-based sweep ──────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_revoke_scoped_to_lease_subnet(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expiry sweep must delete only the expiring lease's own-subnet
    mirror, leaving same-address mirrors in other subnets in place."""
    import app.tasks.dhcp_lease_cleanup as cleanup

    grp, srv = await _make_server(db_session)
    subnet_a, mirror_a = await _make_subnet_with_mirror(
        db_session, block_cidr="10.0.0.0/16", subnet_cidr="10.0.0.0/24", ip=SHARED_IP
    )
    subnet_b, mirror_b = await _make_subnet_with_mirror(
        db_session, block_cidr="172.16.0.0/16", subnet_cidr="172.16.5.0/24", ip=SHARED_IP
    )
    scope_a = DHCPScope(group_id=grp.id, subnet_id=subnet_a.id, is_active=True)
    db_session.add(scope_a)
    await db_session.flush()
    past = datetime.now(UTC) - timedelta(hours=1)
    lease = DHCPLease(
        server_id=srv.id,
        scope_id=scope_a.id,
        ip_address=SHARED_IP,
        mac_address="aa:bb:cc:dd:ee:01",
        state="active",
        expires_at=past,
    )
    db_session.add(lease)
    await db_session.commit()

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    import app.services.dns.ddns as ddns

    monkeypatch.setattr(ddns, "revoke_ddns_for_lease", _noop)

    cleaned, _deleted = await cleanup._sweep()
    assert cleaned == 1

    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror_a.id))
    ).scalar_one_or_none() is None
    survivor = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror_b.id))
    ).scalar_one_or_none()
    assert survivor is not None
    assert survivor.subnet_id == subnet_b.id
