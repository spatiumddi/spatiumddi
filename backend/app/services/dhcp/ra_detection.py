"""Rogue IPv6 Router-Advertisement classification + upsert (issue #524).

The IPv6 twin of ``app.services.dhcp.rogue_detection``. The DHCP agent's opt-in
passive RA sniffer ships every ICMPv6 Router Advertisement (type 134) it sees to
``POST /dhcp/agents/ra-observations``; this module classifies each source router
against the group's expected-router allowlist and upserts a ``ra_observed_router``
row. The ``rogue_ra`` alert fires on rows that land ``classification == "rogue"``.

Unlike rogue-DHCP, RA has no "known group member" inventory to compare against —
a router is *expected* iff its source IP or source MAC is on the group's RA
allowlist, otherwise it is *rogue*.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import RAObservedRouter, RARouterAllowlist


@dataclass
class ObservedRA:
    """One Router Advertisement the agent saw on the wire."""

    source_ip: str
    source_mac: str | None = None
    prefixes: list[str] = field(default_factory=list)
    managed_flag: bool = False
    other_flag: bool = False
    router_lifetime: int | None = None
    iface: str | None = None


def _norm_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    return str(mac).strip().lower() or None


def _classify(
    allow: Sequence[RARouterAllowlist],
    source_ip: str,
    source_mac: str | None,
) -> str:
    """Pure classification of one RA source against a preloaded allowlist.

    An allowlist entry that pins a ``source_mac`` only blesses observations
    from that MAC: an IP-only entry (no MAC) still matches by IP — the
    operator's explicit choice — but when an entry pins MAC-A and the observed
    MAC is MAC-B on the same link-local IP we do NOT auto-classify expected, so
    a genuine rogue sharing a common ``fe80::1`` still fires the alert.

    Residual limitation: an attacker that *also* spoofs the allowlisted MAC on
    the same link cannot be distinguished — link-local IP + MAC is the finest
    identity the passive RA sniffer sees on the wire.
    """
    mac = _norm_mac(source_mac)
    for a in allow:
        allow_mac = _norm_mac(str(a.source_mac)) if a.source_mac else None
        if a.source_ip and str(a.source_ip) == source_ip:
            # IP matched. Bless only when the entry pins no MAC (IP-only) or the
            # pinned MAC matches the observed one.
            if allow_mac is None or allow_mac == mac:
                return "expected"
        elif allow_mac is not None and allow_mac == mac:
            # MAC-only allowlist entry (or an entry whose IP didn't match but
            # whose MAC does) — bless by MAC.
            return "expected"
    return "rogue"


async def classify_router(
    db: AsyncSession,
    group_id: uuid.UUID,
    source_ip: str,
    source_mac: str | None,
    allow: Sequence[RARouterAllowlist] | None = None,
) -> str:
    """Return ``expected`` (on allowlist) or ``rogue`` for one RA source.

    ``allow`` may be preloaded by the caller (``record_observations`` loads the
    group's allowlist once per batch since ``group_id`` is constant); when None
    it is queried here.
    """
    if allow is None:
        allow = (
            (
                await db.execute(
                    select(RARouterAllowlist).where(RARouterAllowlist.group_id == group_id)
                )
            )
            .scalars()
            .all()
        )
    return _classify(allow, source_ip, source_mac)


async def record_observations(
    db: AsyncSession, server, observations: list[ObservedRA]
) -> dict[str, int]:
    """Classify + upsert each observed RA. Commits. Returns per-class counts.

    A standalone server with no group has no allowlist context, so we skip.
    """
    counts = {"expected": 0, "acknowledged": 0, "rogue": 0, "skipped": 0}
    group_id = server.server_group_id
    if group_id is None:
        counts["skipped"] = len(observations)
        return counts

    # Load the group's allowlist once — group_id is constant for the batch, so
    # re-querying per observation was pure overhead.
    allow = (
        (await db.execute(select(RARouterAllowlist).where(RARouterAllowlist.group_id == group_id)))
        .scalars()
        .all()
    )

    now = datetime.now(UTC)
    for o in observations:
        if not o.source_ip:
            counts["skipped"] += 1
            continue
        cls = await classify_router(db, group_id, o.source_ip, o.source_mac, allow=allow)
        # Identity is (group, source_ip, source_mac) — two physically distinct
        # routers sharing a link-local IP get distinct rows. A NULL source_mac
        # is its own bucket (matched with IS NULL, since UNIQUE treats NULLs as
        # distinct).
        mac_filter = (
            RAObservedRouter.source_mac.is_(None)
            if o.source_mac is None
            else RAObservedRouter.source_mac == o.source_mac
        )
        existing = (
            await db.execute(
                select(RAObservedRouter).where(
                    RAObservedRouter.group_id == group_id,
                    RAObservedRouter.source_ip == o.source_ip,
                    mac_filter,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.last_seen_at = now
            existing.source_mac = o.source_mac or existing.source_mac
            existing.prefixes = list(o.prefixes or existing.prefixes)
            existing.managed_flag = o.managed_flag
            existing.other_flag = o.other_flag
            existing.router_lifetime = o.router_lifetime
            existing.iface = o.iface or existing.iface
            existing.reported_by_server_id = server.id
            # Don't downgrade an operator's ``acknowledged`` back to rogue.
            if existing.classification != "acknowledged":
                existing.classification = cls
            counts[existing.classification if existing.classification in counts else cls] += 1
        else:
            db.add(
                RAObservedRouter(
                    group_id=group_id,
                    reported_by_server_id=server.id,
                    source_ip=o.source_ip,
                    source_mac=o.source_mac,
                    prefixes=list(o.prefixes or []),
                    managed_flag=o.managed_flag,
                    other_flag=o.other_flag,
                    router_lifetime=o.router_lifetime,
                    iface=o.iface,
                    classification=cls,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            counts[cls] += 1
    await db.commit()
    return counts


__all__ = ["ObservedRA", "classify_router", "record_observations"]
