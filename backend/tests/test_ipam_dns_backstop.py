"""#428 — DDNS-aware IPAM↔DNS backstop helpers.

The scheduled ``ipam_dns_sync`` backstop must (a) honor the DDNS opt-in —
only DNS-publish lease-mirrored rows for subnets whose DDNS is enabled
(manual allocations always sync) — and (b) for DDNS-enabled subnets,
re-run the DDNS policy over active lease mirrors so a record a swallowed
inline apply missed (incl. a generated ``dhcp-<x-y>`` name) is
regenerated. These tests pin the two new helpers.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.tasks.ipam_dns_sync import _auto_from_lease_ip_ids, _reapply_ddns_for_active_leases


async def _subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"bk-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.80.0.0/16", name="bk-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.80.1.0/24", name="bk-sn")
    db.add(subnet)
    await db.flush()
    return subnet


async def _add_ip(db: AsyncSession, subnet: Subnet, addr: str, *, lease: bool) -> IPAddress:
    row = IPAddress(
        subnet_id=subnet.id,
        address=addr,
        status="dhcp" if lease else "allocated",
        hostname="h" if lease else "manual-host",
        mac_address="aa:bb:cc:dd:ee:01",
        auto_from_lease=lease,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_auto_from_lease_ip_ids_only_returns_lease_rows(db_session: AsyncSession) -> None:
    subnet = await _subnet(db_session)
    lease_row = await _add_ip(db_session, subnet, "10.80.1.10", lease=True)
    await _add_ip(db_session, subnet, "10.80.1.20", lease=False)  # manual
    await db_session.commit()

    ids = await _auto_from_lease_ip_ids(db_session, subnet.id)
    assert ids == {lease_row.id}


@pytest.mark.asyncio
async def test_reapply_ddns_iterates_only_lease_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    subnet = await _subnet(db_session)
    await _add_ip(db_session, subnet, "10.80.1.10", lease=True)
    await _add_ip(db_session, subnet, "10.80.1.11", lease=True)
    await _add_ip(db_session, subnet, "10.80.1.20", lease=False)  # manual — skipped
    await db_session.commit()

    seen: list[str] = []

    async def _fake_apply(db, *, subnet, ipam_row, client_hostname):  # type: ignore[no-untyped-def]
        seen.append(str(ipam_row.address))
        return True  # pretend a record was (re)published

    # The helper lazy-imports apply_ddns_for_lease from this module.
    import app.services.dns.ddns as ddns_mod

    monkeypatch.setattr(ddns_mod, "apply_ddns_for_lease", _fake_apply)

    fired, errors = await _reapply_ddns_for_active_leases(db_session, subnet)
    assert fired == 2
    assert errors == []
    assert sorted(seen) == ["10.80.1.10", "10.80.1.11"]  # only the lease rows


@pytest.mark.asyncio
async def test_reapply_ddns_collects_per_row_errors(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    subnet = await _subnet(db_session)
    await _add_ip(db_session, subnet, "10.80.1.10", lease=True)
    await db_session.commit()

    async def _boom(db, *, subnet, ipam_row, client_hostname):  # type: ignore[no-untyped-def]
        raise RuntimeError("zone unreachable")

    import app.services.dns.ddns as ddns_mod

    monkeypatch.setattr(ddns_mod, "apply_ddns_for_lease", _boom)

    fired, errors = await _reapply_ddns_for_active_leases(db_session, subnet)
    assert fired == 0
    assert len(errors) == 1 and "zone unreachable" in errors[0]
