"""Auto-create an enclosing IPBlock for multicast groups.

Multicast groups carry only ``space_id`` — there's no FK to a Block.
But operators expect the IPAM tree to surface the multicast range
(e.g. ``224.0.0.0/4``) the same way it surfaces unicast supernets.
This service ensures a block exists in the group's IPSpace whose
CIDR encloses the group's address; the IPAM tree then renders it
naturally + the block detail view's ``MulticastGroupsPanel``
filters its groups by that CIDR.

Idempotent: if any existing block in the space already encloses the
address, that block is returned and nothing is created. We never
touch operator-created blocks.

Default CIDR when no enclosing block exists:
* IPv4 multicast → ``224.0.0.0/4`` (the full RFC 5771 range)
* IPv6 multicast → ``ff00::/8`` (the full RFC 4291 range)

Operators who want a narrower scope (e.g. just ``239.0.0.0/8`` for
administratively-scoped multicast) create that block manually before
adding their groups; the auto-create then short-circuits because
their block already encloses everything.
"""

from __future__ import annotations

import ipaddress
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPBlock

logger = structlog.get_logger(__name__)

# Well-known multicast supernets used as the auto-create default
# when no enclosing operator block exists.
_DEFAULT_V4_CIDR = "224.0.0.0/4"
_DEFAULT_V6_CIDR = "ff00::/8"
_AUTO_BLOCK_NAME = "Multicast"
_AUTO_BLOCK_DESCRIPTION = (
    "Auto-created to host multicast groups in this IPSpace. "
    "Safe to rename; do not delete unless you've moved the groups elsewhere."
)


def _supernet_for(address: str) -> str | None:
    """Return the well-known multicast supernet CIDR for ``address``,
    or ``None`` if the address isn't multicast."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if not ip.is_multicast:
        return None
    return _DEFAULT_V4_CIDR if ip.version == 4 else _DEFAULT_V6_CIDR


async def ensure_enclosing_block(
    db: AsyncSession,
    space_id: uuid.UUID,
    address: str,
) -> IPBlock | None:
    """Ensure an IPBlock in ``space_id`` encloses ``address``.

    Returns the existing-or-newly-created block, or ``None`` if the
    address isn't a multicast IP (no work to do). Caller is
    responsible for the surrounding transaction commit.
    """
    supernet = _supernet_for(address)
    if supernet is None:
        return None
    target_addr = ipaddress.ip_address(address)

    # Look for any existing block in this space that encloses the
    # address. ``IPBlock.network`` is a CIDR string with the prefix
    # ("239.0.0.0/8"), so we parse + test each one.
    rows = (await db.execute(select(IPBlock).where(IPBlock.space_id == space_id))).scalars().all()
    for blk in rows:
        try:
            net = ipaddress.ip_network(blk.network, strict=False)
        except ValueError:
            continue
        if target_addr.version != net.version:
            continue
        if target_addr in net:
            return blk

    # No enclosing block — create the well-known supernet.
    block = IPBlock(
        space_id=space_id,
        network=supernet,
        name=_AUTO_BLOCK_NAME,
        description=_AUTO_BLOCK_DESCRIPTION,
    )
    db.add(block)
    await db.flush()
    logger.info(
        "multicast_auto_block_created",
        space_id=str(space_id),
        cidr=supernet,
        block_id=str(block.id),
    )
    return block


async def backfill_blocks_for_existing_groups(db: AsyncSession) -> int:
    """One-shot sweep: for every multicast group, ensure its IPSpace
    has an enclosing block. Idempotent — only inserts where missing.
    Returns the count of blocks newly created. Safe to run on every
    startup; cost is O(spaces × groups-per-space).
    """
    # Local import to keep app-boot import graph small.
    from app.models.multicast import MulticastGroup  # noqa: PLC0415

    rows = (await db.execute(select(MulticastGroup))).scalars().all()
    seen: set[tuple[uuid.UUID, str]] = set()
    created = 0
    for g in rows:
        # Dedupe by (space_id, supernet) so we don't query the same
        # space's blocks once per group.
        supernet = _supernet_for(g.address)
        if supernet is None:
            continue
        key = (g.space_id, supernet)
        if key in seen:
            continue
        seen.add(key)
        before = await db.execute(
            select(IPBlock).where(IPBlock.space_id == g.space_id, IPBlock.network == supernet)
        )
        if before.scalar_one_or_none() is not None:
            continue
        block = await ensure_enclosing_block(db, g.space_id, g.address)
        if block is not None and str(block.network) == supernet:
            # Newly created (existing-block path returns the
            # operator block, whose network won't match the
            # supernet by definition).
            created += 1
    if created > 0:
        await db.commit()
    return created
