"""Service layer for DNS blocking lists.

Produces a backend-neutral representation (`EffectiveBlocklist`) of the set of
blocked domains + exceptions that apply to a given DNS view or server group.
The DNS driver layer (BIND9 RPZ emitter Lua emitter, etc.) consumes
this structure to generate actual server config.

Driver-abstraction rule (CLAUDE.md #10): no BIND9 specifics live in
this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dns import (
    DNSBlockList,
    DNSBlockListEntry,
    DNSBlockListException,
    DNSServerGroup,
    DNSView,
)


@dataclass(frozen=True)
class EffectiveEntry:
    """Backend-neutral representation of a blocked domain entry."""

    domain: str
    # action: block | redirect | nxdomain
    action: str
    # block_mode inherited from the list: nxdomain | sinkhole | refused
    block_mode: str
    sinkhole_ip: str | None
    target: str | None
    is_wildcard: bool
    list_id: uuid.UUID
    list_name: str


@dataclass
class EffectiveBlocklist:
    """The set of effective entries and exceptions for a given scope.

    The driver iterates `entries`, skipping any domain in `exceptions`.
    """

    scope: str  # "view" | "group"
    scope_id: uuid.UUID
    entries: list[EffectiveEntry] = field(default_factory=list)
    exceptions: set[str] = field(default_factory=set)
    lists: list[uuid.UUID] = field(default_factory=list)


async def _collect_lists(
    db: AsyncSession, lists: list[DNSBlockList]
) -> tuple[list[EffectiveEntry], set[str], list[uuid.UUID]]:
    entries: list[EffectiveEntry] = []
    exceptions: set[str] = set()
    list_ids: list[uuid.UUID] = []

    for bl in lists:
        if not bl.enabled:
            continue
        list_ids.append(bl.id)

        entry_result = await db.execute(
            select(DNSBlockListEntry).where(DNSBlockListEntry.list_id == bl.id)
        )
        for e in entry_result.scalars().all():
            entries.append(
                EffectiveEntry(
                    domain=e.domain.lower(),
                    action=e.entry_type,
                    block_mode=bl.block_mode,
                    sinkhole_ip=bl.sinkhole_ip,
                    target=e.target,
                    is_wildcard=e.is_wildcard,
                    list_id=bl.id,
                    list_name=bl.name,
                )
            )

        exc_result = await db.execute(
            select(DNSBlockListException).where(DNSBlockListException.list_id == bl.id)
        )
        for ex in exc_result.scalars().all():
            exceptions.add(ex.domain.lower())

    return entries, exceptions, list_ids


async def build_effective_for_view(db: AsyncSession, view_id: uuid.UUID) -> EffectiveBlocklist:
    """Compute the effective blocklist for a DNS view.

    Combines:
      - Blocklists assigned directly to the view
      - Blocklists assigned to the view's parent server group
    """
    view = (
        await db.execute(
            select(DNSView)
            .where(DNSView.id == view_id)
            .options(
                selectinload(DNSView.blocklists),
                selectinload(DNSView.group).selectinload(DNSServerGroup.blocklists),
            )
        )
    ).scalar_one_or_none()

    if view is None:
        return EffectiveBlocklist(scope="view", scope_id=view_id)

    combined = {bl.id: bl for bl in view.blocklists}
    if view.group is not None:
        for bl in view.group.blocklists:
            combined.setdefault(bl.id, bl)

    entries, exceptions, list_ids = await _collect_lists(db, list(combined.values()))
    return EffectiveBlocklist(
        scope="view",
        scope_id=view_id,
        entries=entries,
        exceptions=exceptions,
        lists=list_ids,
    )


async def build_effective_for_group(db: AsyncSession, group_id: uuid.UUID) -> EffectiveBlocklist:
    """Compute the effective blocklist for a DNS server group (all views)."""
    group = (
        await db.execute(
            select(DNSServerGroup)
            .where(DNSServerGroup.id == group_id)
            .options(selectinload(DNSServerGroup.blocklists))
        )
    ).scalar_one_or_none()

    if group is None:
        return EffectiveBlocklist(scope="group", scope_id=group_id)

    entries, exceptions, list_ids = await _collect_lists(db, list(group.blocklists))
    return EffectiveBlocklist(
        scope="group",
        scope_id=group_id,
        entries=entries,
        exceptions=exceptions,
        lists=list_ids,
    )


# ── Feed parsing (manual / hosts / domains / adblock) ────────────────────────


def parse_feed(content: str, feed_format: str) -> list[str]:
    """Parse raw feed text into a deduped list of domains.

    Accepts:
      - `hosts`: `0.0.0.0 ads.example.com` (or `127.0.0.1`)
      - `domains`: one domain per line
      - `adblock`: `||ads.example.com^`

    Ignores blank lines and comments (`#`, `!`).
    """
    out: list[str] = []
    seen: set[str] = set()

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue

        domain: str | None = None

        if feed_format == "adblock":
            # ||ads.example.com^  or ||ads.example.com
            if line.startswith("||"):
                rest = line[2:]
                for sep in ("^", "$", "/"):
                    idx = rest.find(sep)
                    if idx != -1:
                        rest = rest[:idx]
                        break
                domain = rest
        elif feed_format == "hosts":
            # Strip inline comment
            line = line.split("#", 1)[0].strip()
            parts = line.split()
            if len(parts) >= 2:
                domain = parts[1]
            elif len(parts) == 1:
                domain = parts[0]
        else:  # "domains"
            line = line.split("#", 1)[0].strip()
            if line:
                domain = line.split()[0]

        if not domain:
            continue
        domain = domain.lower().strip(".")
        if not domain or "." not in domain:
            continue
        if domain in seen:
            continue
        seen.add(domain)
        out.append(domain)

    return out


def dedupe_domains(domains: list[str]) -> list[str]:
    """Return a deduped, lowercased, sorted-by-input-order list of valid domains."""
    seen: set[str] = set()
    out: list[str] = []
    for d in domains:
        dd = d.strip().lower().strip(".")
        if not dd or "." not in dd or dd in seen:
            continue
        seen.add(dd)
        out.append(dd)
    return out
