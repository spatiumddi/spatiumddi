"""RPZ hit attribution — which clients keep reaching for blocked names (#699).

We serve curated blocklists as RPZ zones and, before this, discarded
every hit. That left the most operationally useful thing the blocklists
can tell you unanswerable: a blocked lookup is not a problem, but a
host generating thousands of them is an infected machine announcing
itself, and we were throwing that away.

Pure reporting — unlike the other three detections there is no scoring
here, because there is nothing to infer. A hit is ground truth: named
matched a policy and said so. The job is only to attribute hits to
clients and feeds and rank them.

**PASSTHRU is not a block.** It is an explicit allow — an exception
entry that let a name through *past* a blocklist. Counting it as a
block would inflate every client's number and, worse, would make a
correctly-configured allowlist look like an infection. Every function
here separates the two.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns_rpz_hit import DNSRPZHit

# An explicit allow, not a block. See the module docstring.
PASSTHRU = "PASSTHRU"

# A client has to be reaching for blocked names with some persistence
# before it is worth an operator's attention. One hit is an ad on a web
# page; hundreds in a day is a host with something running on it.
NOISY_CLIENT_HITS = 50


async def top_offending_clients(
    db: AsyncSession,
    *,
    hours: int = 24,
    limit: int = 20,
    min_hits: int = 1,
) -> list[dict[str, Any]]:
    """Clients ranked by blocked-lookup count over the trailing window.

    Rows with no ``client_ip`` are excluded: a hit we could not attribute
    still counts toward the totals (it proves the blocklist fired) but
    cannot be pinned to a host, and listing it as an anonymous offender
    would be noise. :func:`summary` keeps it.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = (
        select(
            DNSRPZHit.client_ip.label("client_ip"),
            func.count().label("hits"),
            func.count(func.distinct(DNSRPZHit.qname)).label("distinct_names"),
            func.count(func.distinct(DNSRPZHit.rpz_zone)).label("distinct_feeds"),
            func.max(DNSRPZHit.ts).label("last_seen"),
        )
        .where(
            DNSRPZHit.ts >= since,
            DNSRPZHit.client_ip.is_not(None),
            DNSRPZHit.policy != PASSTHRU,
        )
        .group_by(DNSRPZHit.client_ip)
        .having(func.count() >= min_hits)
        .order_by(func.count().desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    out: list[dict[str, Any]] = []
    for r in rows:
        ip = str(r.client_ip)
        # The single name this client hit hardest — the detail that
        # turns "10.0.0.5 made 4,000 blocked lookups" into something an
        # operator can actually chase.
        worst = (
            await db.execute(
                select(DNSRPZHit.qname, func.count().label("n"))
                .where(
                    DNSRPZHit.ts >= since,
                    DNSRPZHit.client_ip == ip,
                    DNSRPZHit.policy != PASSTHRU,
                    DNSRPZHit.qname.is_not(None),
                )
                .group_by(DNSRPZHit.qname)
                .order_by(func.count().desc())
                .limit(1)
            )
        ).first()
        out.append(
            {
                "client_ip": ip,
                "hits": r.hits,
                "distinct_names": r.distinct_names,
                "distinct_feeds": r.distinct_feeds,
                "last_seen": r.last_seen,
                "top_qname": worst.qname if worst else None,
                "top_qname_hits": worst.n if worst else 0,
                "noisy": r.hits >= NOISY_CLIENT_HITS,
            }
        )
    return out


async def top_blocked_names(
    db: AsyncSession, *, hours: int = 24, limit: int = 20
) -> list[dict[str, Any]]:
    """The blocked names themselves, ranked by hit count."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(
                DNSRPZHit.qname.label("qname"),
                func.count().label("hits"),
                func.count(func.distinct(DNSRPZHit.client_ip)).label("clients"),
                func.min(DNSRPZHit.rpz_zone).label("rpz_zone"),
            )
            .where(
                DNSRPZHit.ts >= since,
                DNSRPZHit.qname.is_not(None),
                DNSRPZHit.policy != PASSTHRU,
            )
            .group_by(DNSRPZHit.qname)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "qname": r.qname,
            "hits": r.hits,
            "clients": r.clients,
            "rpz_zone": r.rpz_zone,
        }
        for r in rows
    ]


async def feed_effectiveness(db: AsyncSession, *, hours: int = 24) -> list[dict[str, Any]]:
    """Hits per blocklist feed — which lists are actually earning their keep.

    Operators subscribe to feeds and never learn which ones fire. A feed
    with zero hits over a month is a candidate for removal; one carrying
    most of the blocking is a candidate for keeping through a
    consolidation.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(
                DNSRPZHit.rpz_zone.label("rpz_zone"),
                func.count().label("hits"),
                func.count(func.distinct(DNSRPZHit.client_ip)).label("clients"),
                func.count(func.distinct(DNSRPZHit.qname)).label("distinct_names"),
            )
            .where(DNSRPZHit.ts >= since, DNSRPZHit.policy != PASSTHRU)
            .group_by(DNSRPZHit.rpz_zone)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        {
            "rpz_zone": r.rpz_zone,
            "hits": r.hits,
            "clients": r.clients,
            "distinct_names": r.distinct_names,
        }
        for r in rows
    ]


async def summary(db: AsyncSession, *, hours: int = 24) -> dict[str, Any]:
    """Rollup for the dashboard card and the Operator Copilot.

    ``has_data`` distinguishes "nothing was blocked" from "this isn't
    running" — the same contract the threat summary carries, and for the
    same reason: an idle feature rendering as an all-clear is exactly
    the failure this whole area exists to avoid. Here it is sharper than
    usual, because zero hits is a *plausible* real answer on a quiet
    network, so the UI must not have to guess.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    totals = (
        await db.execute(
            select(
                func.count().label("hits"),
                func.count(func.distinct(DNSRPZHit.client_ip)).label("clients"),
                func.count(func.distinct(DNSRPZHit.qname)).label("names"),
                func.count(func.distinct(DNSRPZHit.rpz_zone)).label("feeds"),
            ).where(DNSRPZHit.ts >= since, DNSRPZHit.policy != PASSTHRU)
        )
    ).one()
    # Counted separately rather than filtered out entirely: an operator
    # tuning an allowlist wants to see it working.
    passthru = (
        await db.execute(
            select(func.count()).where(DNSRPZHit.ts >= since, DNSRPZHit.policy == PASSTHRU)
        )
    ).scalar_one()
    # Any hit at all — including PASSTHRU and unattributable rows — is
    # proof the pipeline is alive, which is what has_data must mean.
    any_row = (await db.execute(select(func.count()).where(DNSRPZHit.ts >= since))).scalar_one()
    top = await top_offending_clients(db, hours=hours, limit=1)
    return {
        "blocked_hits": totals.hits or 0,
        "clients_blocked": totals.clients or 0,
        "distinct_names": totals.names or 0,
        "feeds_firing": totals.feeds or 0,
        "passthru_hits": passthru or 0,
        "worst_client_ip": top[0]["client_ip"] if top else None,
        "worst_client_hits": top[0]["hits"] if top else None,
        "since": since,
        "has_data": (any_row or 0) > 0,
    }
