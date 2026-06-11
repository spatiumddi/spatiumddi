"""Rogue DHCP responder classification + upsert (issue #370).

The agent's active probe broadcasts a DISCOVER and ships every OFFER it sees to
``POST /dhcp/agents/dhcp-offers``. This module classifies each observed
responder against the group's known DHCP servers + the operator allowlist and
upserts a ``dhcp_observed_responder`` row. The ``rogue_dhcp`` alert fires on the
rows that land ``classification == "rogue"``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPObservedResponder, DHCPResponderAllowlist, DHCPServer


@dataclass
class ObservedOffer:
    """One OFFER the agent saw on the wire."""

    server_identifier: str
    source_ip: str
    source_mac: str | None = None
    giaddr: str | None = None
    offered_ip: str | None = None


async def _known_responder_keys(db: AsyncSession, group_id: uuid.UUID) -> set[str]:
    """The set of IPs / identifiers that count as an *expected* responder for a
    group — the configured ``host`` and the last-seen IP of every member.

    ``host`` may be a hostname rather than an IP; ``last_seen_ip`` is the
    reliable key and is populated on the member's first heartbeat (within ~60s
    of the agent starting). So a member whose ``host`` is a hostname and which
    hasn't heartbeated yet could briefly mis-classify its own OFFER as rogue —
    self-heals on the next heartbeat, and the operator can acknowledge meanwhile.
    """
    members = (
        (await db.execute(select(DHCPServer).where(DHCPServer.server_group_id == group_id)))
        .scalars()
        .all()
    )
    keys: set[str] = set()
    for m in members:
        if m.host:
            keys.add(str(m.host))
        if getattr(m, "last_seen_ip", None):
            keys.add(str(m.last_seen_ip))
    return keys


async def classify_responder(
    db: AsyncSession,
    group_id: uuid.UUID,
    server_identifier: str,
    source_ip: str,
) -> str:
    """Return ``expected`` / ``acknowledged`` / ``rogue`` for one responder."""
    known = await _known_responder_keys(db, group_id)
    if source_ip in known or server_identifier in known:
        return "expected"
    allow = (
        (
            await db.execute(
                select(DHCPResponderAllowlist).where(DHCPResponderAllowlist.group_id == group_id)
            )
        )
        .scalars()
        .all()
    )
    for a in allow:
        if a.server_identifier and a.server_identifier == server_identifier:
            return "acknowledged"
        if a.source_ip and str(a.source_ip) == source_ip:
            return "acknowledged"
    return "rogue"


async def record_offers(
    db: AsyncSession, server: DHCPServer, offers: list[ObservedOffer]
) -> dict[str, int]:
    """Classify + upsert each observed offer. Commits. Returns per-class counts.

    A standalone server with no group has no inventory context to classify
    against, so we skip (the agent simply won't have anything to compare to).
    """
    counts = {"expected": 0, "acknowledged": 0, "rogue": 0, "skipped": 0}
    group_id = server.server_group_id
    if group_id is None:
        counts["skipped"] = len(offers)
        return counts

    from datetime import UTC, datetime  # noqa: PLC0415

    now = datetime.now(UTC)
    for o in offers:
        if not o.server_identifier or not o.source_ip:
            counts["skipped"] += 1
            continue
        cls = await classify_responder(db, group_id, o.server_identifier, o.source_ip)
        existing = (
            await db.execute(
                select(DHCPObservedResponder).where(
                    DHCPObservedResponder.group_id == group_id,
                    DHCPObservedResponder.server_identifier == o.server_identifier,
                    DHCPObservedResponder.source_ip == o.source_ip,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.last_seen_at = now
            existing.source_mac = o.source_mac or existing.source_mac
            existing.giaddr = o.giaddr or existing.giaddr
            existing.offered_ip = o.offered_ip or existing.offered_ip
            existing.reported_by_server_id = server.id
            # Don't downgrade an operator's ``acknowledged`` back to rogue —
            # the allowlist is the authority for that. Otherwise refresh.
            if existing.classification != "acknowledged":
                existing.classification = cls
        else:
            db.add(
                DHCPObservedResponder(
                    group_id=group_id,
                    reported_by_server_id=server.id,
                    server_identifier=o.server_identifier,
                    source_ip=o.source_ip,
                    source_mac=o.source_mac,
                    giaddr=o.giaddr,
                    offered_ip=o.offered_ip,
                    classification=cls,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        counts[cls] += 1
    await db.commit()
    return counts
