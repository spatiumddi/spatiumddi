"""#564 — DHCP lease→IPAM mirror insert race guard.

Several DHCP lease-ingestion paths mirror a lease into an
``ip_address`` row with an unguarded ``SELECT; if None: INSERT``.
Under concurrency two writers both see "no row", both INSERT, and the
loser hits ``uq_ip_address_subnet_address`` — a 500 whose
``PendingRollbackError`` tail poisons the rest of the batch.

``insert_ipam_mirror_row`` makes the INSERT idempotent: it attempts
the insert inside a SAVEPOINT so a unique-violation rolls back only
the nested transaction (leaving the outer session usable) and returns
the row a concurrent writer already committed.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.dhcp.ipam_mirror import insert_ipam_mirror_row


async def _make_subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.9.0.0/16", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.9.0.0/24", name="sn")
    db.add(subnet)
    await db.flush()
    return subnet


@pytest.mark.asyncio
async def test_insert_creates_when_absent(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    row, created = await insert_ipam_mirror_row(
        db_session,
        IPAddress(subnet_id=subnet.id, address="10.9.0.10", status="dhcp", auto_from_lease=True),
    )
    assert created is True
    assert row.id is not None  # flush inside the savepoint assigned the PK
    assert str(row.address) == "10.9.0.10"


@pytest.mark.asyncio
async def test_insert_returns_incumbent_on_conflict(db_session: AsyncSession) -> None:
    """A committed row at (subnet, address) → the second insert
    self-heals into the incumbent instead of raising on the unique
    constraint, and the outer session stays usable."""
    subnet = await _make_subnet(db_session)
    incumbent = IPAddress(
        subnet_id=subnet.id,
        address="10.9.0.20",
        status="static_dhcp",
        mac_address="aa:bb:cc:dd:ee:01",
    )
    db_session.add(incumbent)
    await db_session.commit()

    # A fresh candidate object for the SAME (subnet_id, address) — the
    # concurrent-writer shape. The DB already holds the incumbent.
    row, created = await insert_ipam_mirror_row(
        db_session,
        IPAddress(subnet_id=subnet.id, address="10.9.0.20", status="dhcp", auto_from_lease=True),
    )
    assert created is False
    assert row.id == incumbent.id  # the committed row came back
    assert row.status == "static_dhcp"  # untouched — caller decides how to update

    # The outer session survived the rolled-back savepoint (no
    # PendingRollbackError tail) — a follow-up insert + commit works.
    other, other_created = await insert_ipam_mirror_row(
        db_session,
        IPAddress(subnet_id=subnet.id, address="10.9.0.21", status="dhcp", auto_from_lease=True),
    )
    assert other_created is True
    await db_session.commit()
    assert (
        await db_session.execute(select(IPAddress).where(IPAddress.id == other.id))
    ).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_unrelated_integrity_error_reraises(db_session: AsyncSession) -> None:
    """An IntegrityError that ISN'T our (subnet, address) tuple (here a
    bogus subnet_id FK) re-raises rather than being masked as a
    self-heal — re-selecting the pair finds nothing."""
    bogus = IPAddress(subnet_id=uuid.uuid4(), address="10.9.0.30", status="dhcp")
    with pytest.raises(IntegrityError):
        await insert_ipam_mirror_row(db_session, bogus)
