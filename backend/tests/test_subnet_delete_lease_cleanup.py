"""#428 — subnet soft-delete revokes DHCP-lease DDNS records + mirrors.

The soft-delete batch only stamps the subnet + its DHCP scopes, so a
lease-mirrored IPAM row's published DDNS A/PTR record would otherwise be
orphaned (still on the DNS server, pointing at a deleted subnet, hidden
from the sweeps). ``_revoke_subnet_lease_mirrors`` revokes those records
and drops the transient mirror rows; manual allocations are untouched.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.ipam.router import _revoke_subnet_lease_mirrors
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


async def _subnet_with_rows(db: AsyncSession) -> tuple[Subnet, list[str]]:
    space = IPSpace(name=f"sd-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.90.0.0/16", name="sd-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.90.1.0/24", name="sd-sn")
    db.add(subnet)
    await db.flush()
    for addr, lease in (("10.90.1.10", True), ("10.90.1.11", True), ("10.90.1.20", False)):
        db.add(
            IPAddress(
                subnet_id=subnet.id,
                address=addr,
                status="dhcp" if lease else "allocated",
                mac_address="aa:bb:cc:dd:ee:01",
                hostname="h",
                auto_from_lease=lease,
            )
        )
    await db.flush()
    return subnet, ["10.90.1.10", "10.90.1.11"]


@pytest.mark.asyncio
async def test_revoke_subnet_lease_mirrors(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    subnet, lease_ips = await _subnet_with_rows(db_session)
    await db_session.commit()

    revoked_ips: list[str] = []

    async def _fake_revoke(db, *, subnet, ipam_row):  # type: ignore[no-untyped-def]
        revoked_ips.append(str(ipam_row.address))
        return True

    import app.services.dns.ddns as ddns_mod

    monkeypatch.setattr(ddns_mod, "revoke_ddns_for_lease", _fake_revoke)

    count = await _revoke_subnet_lease_mirrors(db_session, subnet)
    await db_session.flush()

    # Both lease mirrors revoked + deleted; the manual allocation survives.
    assert count == 2
    assert sorted(revoked_ips) == lease_ips
    remaining = (
        (
            await db_session.execute(
                select(IPAddress.address).where(IPAddress.subnet_id == subnet.id)
            )
        )
        .scalars()
        .all()
    )
    assert [str(a) for a in remaining] == ["10.90.1.20"]


@pytest.mark.asyncio
async def test_revoke_is_best_effort_on_dns_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A DNS-revoke failure must not block the delete — the mirror row is
    # still dropped (the reconcile cleans any DNS residue later).
    subnet, _ = await _subnet_with_rows(db_session)
    await db_session.commit()

    async def _boom(db, *, subnet, ipam_row):  # type: ignore[no-untyped-def]
        raise RuntimeError("dns down")

    import app.services.dns.ddns as ddns_mod

    monkeypatch.setattr(ddns_mod, "revoke_ddns_for_lease", _boom)

    count = await _revoke_subnet_lease_mirrors(db_session, subnet)
    await db_session.flush()
    assert count == 2
    lease_rows = (
        (
            await db_session.execute(
                select(IPAddress).where(
                    IPAddress.subnet_id == subnet.id, IPAddress.auto_from_lease.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    assert lease_rows == []
