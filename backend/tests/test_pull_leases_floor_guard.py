"""#482 — zero-wire lease-pull floor guard.

An empty ``get_leases`` response is indistinguishable from a transient driver
hiccup that returned ``[]`` without raising (e.g. Get-DhcpServerv4Scope briefly
reporting no scopes — "empty" isn't an error, so the driver's parse-reraise
doesn't fire). Absence-delete would then purge EVERY tracked lease + IPAM
mirror on that single poll. The floor guard skips the absence-delete for an
empty wire and records a soft error; the time-based ``dhcp_lease_cleanup``
expiry sweep still reclaims genuinely-removed leases once they pass expires_at.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPLease, DHCPScope, DHCPServer, DHCPServerGroup
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


class _StubDriver:
    def __init__(self, leases: list[dict]) -> None:
        self._leases = leases

    async def get_leases(self, _server: DHCPServer) -> list[dict]:
        return self._leases


def _patch(monkeypatch: pytest.MonkeyPatch, leases: list[dict]) -> None:
    from app.services.dhcp import pull_leases as pl

    monkeypatch.setattr(pl, "get_driver", lambda _drv: _StubDriver(leases))
    monkeypatch.setattr(pl, "is_agentless", lambda _drv: True)
    import app.services.dns.ddns as ddns

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ddns, "apply_ddns_for_lease", _noop)
    monkeypatch.setattr(ddns, "revoke_ddns_for_lease", _noop)


async def _server_with_active_lease(
    db: AsyncSession,
) -> tuple[DHCPServer, DHCPLease, IPAddress]:
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(grp)
    await db.flush()
    srv = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}",
        driver="windows_dhcp",
        host="127.0.0.1",
        port=67,
        server_group_id=grp.id,
    )
    db.add(srv)
    await db.flush()
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/16", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="sn")
    db.add(subnet)
    await db.flush()
    scope = DHCPScope(group_id=grp.id, subnet_id=subnet.id, is_active=True)
    db.add(scope)
    await db.flush()
    mirror = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.50",
        status="dhcp",
        mac_address="aa:bb:cc:dd:ee:01",
        auto_from_lease=True,
    )
    db.add(mirror)
    await db.flush()
    lease = DHCPLease(
        server_id=srv.id,
        scope_id=scope.id,
        ip_address="10.0.0.50",
        mac_address="aa:bb:cc:dd:ee:01",
        state="active",
    )
    db.add(lease)
    await db.flush()
    return srv, lease, mirror


@pytest.mark.asyncio
async def test_empty_wire_skips_absence_delete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dhcp import pull_leases as pl

    srv, lease, mirror = await _server_with_active_lease(db_session)
    await db_session.commit()

    _patch(monkeypatch, [])  # empty wire
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    # Nothing deleted; a soft error is recorded so the operator sees why.
    assert result.removed == 0
    assert result.ipam_revoked == 0
    assert any("absence-delete" in e for e in result.errors)
    # Lease + mirror survive the empty poll.
    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.id == lease.id))
    ).scalar_one_or_none() is not None
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror.id))
    ).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_nonempty_wire_still_absence_deletes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dhcp import pull_leases as pl

    srv, lease, mirror = await _server_with_active_lease(db_session)
    await db_session.commit()

    # A non-empty wire that EXCLUDES the tracked lease → it's absent and gets
    # deleted (the guard only skips a wholly-empty wire).
    _patch(monkeypatch, [{"ip_address": "203.0.113.9", "mac_address": "aa:bb:cc:dd:ee:fd"}])
    result = await pl.pull_leases_from_server(db_session, srv, apply=True)
    await db_session.commit()

    assert result.removed == 1
    assert result.ipam_revoked == 1
    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.id == lease.id))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == mirror.id))
    ).scalar_one_or_none() is None
