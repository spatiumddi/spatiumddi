"""Curated DNSBL catalog + idempotent startup seeding (#528).

Mirrors the BGP-communities / RPZ-source catalog pattern: a module-level
tuple of well-known rows, seeded as platform rows (``is_builtin=True``)
keyed on the unique ``zone_suffix``. Re-run on every boot; an operator
who toggles ``enabled`` or edits a row is never clobbered for the
operator-mutable fields (we only refresh the descriptive metadata).

Every catalog list ships **disabled** — the operator opts each list in
from the setup UI after reading its ``requires_registration`` / ``qps_note``
policy. Combined with the master ``dnsbl_monitoring_enabled`` sweep gate,
this guarantees the module makes zero off-prem DNS queries until an
explicit opt-in (non-negotiable spirit of #13 / discovery-without-calls).
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.dnsbl import DNSBLList

logger = structlog.get_logger(__name__)


# Each entry:
#   (name, zone_suffix, category, return_codes, requires_registration,
#    qps_note, homepage_url, description)
#
# ``return_codes`` maps the 127.0.0.x A answer a list returns to a human
# meaning. Kept deliberately small — the common codes operators actually
# see. Unknown codes fall back to the raw address in the UI.
CATALOG: tuple[tuple[str, str, str, dict[str, str], bool, str, str, str], ...] = (
    (
        "Spamhaus ZEN",
        "zen.spamhaus.org",
        "combined",
        {
            "127.0.0.2": "SBL — Spamhaus Blocklist (spam source)",
            "127.0.0.3": "SBL CSS — snowshoe / botnet spam",
            "127.0.0.4": "XBL — exploited / infected (CBL)",
            "127.0.0.9": "SBL DROP / EDROP — hijacked netblock",
            "127.0.0.10": "PBL — end-user / dynamic (ISP policy)",
            "127.0.0.11": "PBL — end-user / dynamic (Spamhaus policy)",
            "127.255.255.252": "typing error / query blocked",
            "127.255.255.254": "public-resolver query refused",
            "127.255.255.255": "excessive queries — rate limited",
        },
        True,
        "Free for low-volume, non-commercial use from a non-public "
        "resolver. Queries from large public resolvers (8.8.8.8, 1.1.1.1) "
        "return 127.255.255.254. High volume / commercial use requires a "
        "Spamhaus Data Query Service (DQS) key or rsync feed.",
        "https://www.spamhaus.org/zen/",
        "Spamhaus combined zone (SBL + XBL + PBL) — the single most "
        "authoritative reputation list. Aggregates known spam sources, "
        "exploited hosts, and end-user/dynamic ranges.",
    ),
    (
        "Barracuda Reputation Block List",
        "b.barracudacentral.org",
        "spam",
        {"127.0.0.2": "listed — poor sending reputation"},
        True,
        "Free to use but requires a one-time registration of the querying "
        "resolver's IP with Barracuda Central before queries resolve.",
        "https://www.barracudacentral.org/rbl",
        "Barracuda Central reputation list — sending IPs with a history "
        "of spam / poor reputation.",
    ),
    (
        "SpamCop Blocking List",
        "bl.spamcop.net",
        "spam",
        {"127.0.0.2": "listed — reported spam source (SCBL)"},
        False,
        "Free for public queries. Fast-expiring, report-driven — an IP "
        "delists automatically once reports age out, so re-check cadence "
        "matters. No registration required for reasonable volume.",
        "https://www.spamcop.net/bl.shtml",
        "SpamCop Blocking List — IPs reported by SpamCop users as spam "
        "sources. Aggressive and fast-decaying.",
    ),
    (
        "SORBS DNSBL",
        "dnsbl.sorbs.net",
        "combined",
        {
            "127.0.0.2": "http — open HTTP proxy",
            "127.0.0.3": "socks — open SOCKS proxy",
            "127.0.0.4": "misc — other open proxy",
            "127.0.0.5": "smtp — open relay",
            "127.0.0.6": "spam — spam source",
            "127.0.0.7": "web — vulnerable web server",
            "127.0.0.8": "block — hijacked / do-not-mail",
            "127.0.0.9": "zombie — hijacked netblock",
            "127.0.0.10": "dul — dynamic IP range",
            "127.0.0.11": "badconf — bad rDNS/config",
            "127.0.0.12": "nomail — operator says never sends mail",
        },
        False,
        "Free for public queries at reasonable volume. Aggregates several "
        "sub-zones (spam, proxy, dul, …) — treat listings conservatively.",
        "http://www.sorbs.net/",
        "SORBS aggregate zone — open proxies/relays, dynamic ranges, and "
        "spam sources across its sub-lists.",
    ),
    (
        "Spamhaus DBL note / UCEPROTECT Level 1",
        "dnsbl-1.uceprotect.net",
        "spam",
        {"127.0.0.2": "listed — spam source (single IP, Level 1)"},
        False,
        "Free for public queries. Level 1 lists single spamming IPs and "
        "expires ~7 days after the last abuse. Levels 2/3 (netblock / AS "
        "wide) are intentionally not included — too broad for per-IP "
        "reputation triage.",
        "https://www.uceprotect.net/",
        "UCEPROTECT Level 1 — individual IPs seen spamming UCEPROTECT "
        "trap addresses. Single-IP scope only.",
    ),
    (
        "Passive Spam Block List (PSBL)",
        "psbl.surriel.com",
        "spam",
        {"127.0.0.2": "listed — reported to PSBL spamtraps"},
        False,
        "Free for public queries. Passive, spamtrap-driven, self-service "
        "delist. Low false-positive rate.",
        "https://psbl.org/",
        "Passive Spam Block List — IPs that hit PSBL spamtraps. "
        "Conservative, easy self-service removal.",
    ),
)


async def seed_dnsbl_catalog() -> None:
    """Insert / refresh the curated catalog rows. Idempotent, keyed on
    ``zone_suffix``. Refreshes descriptive metadata but preserves the
    operator-controlled ``enabled`` flag on existing rows."""
    async with AsyncSessionLocal() as session:
        try:
            existing = (
                (await session.execute(select(DNSBLList).where(DNSBLList.is_builtin.is_(True))))
                .scalars()
                .all()
            )
            by_suffix: dict[str, DNSBLList] = {r.zone_suffix: r for r in existing}
            for (
                name,
                zone_suffix,
                category,
                return_codes,
                requires_registration,
                qps_note,
                homepage_url,
                description,
            ) in CATALOG:
                row = by_suffix.get(zone_suffix)
                if row is None:
                    session.add(
                        DNSBLList(
                            name=name,
                            zone_suffix=zone_suffix,
                            category=category,
                            return_codes=return_codes,
                            requires_registration=requires_registration,
                            qps_note=qps_note,
                            homepage_url=homepage_url,
                            description=description,
                            enabled=False,
                            is_builtin=True,
                        )
                    )
                else:
                    # Refresh descriptive / policy metadata only — never touch
                    # ``enabled`` (operator-owned).
                    row.name = name
                    row.category = category
                    row.return_codes = return_codes
                    row.requires_registration = requires_registration
                    row.qps_note = qps_note
                    row.homepage_url = homepage_url
                    row.description = description
            await session.commit()
        except Exception as exc:  # noqa: BLE001 — never fail boot on this
            logger.debug("dnsbl_catalog_seed_skipped", reason=str(exc))
