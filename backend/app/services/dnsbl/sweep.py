"""DNSBL sweep engine (#528).

Pure helpers (``reversed_octets`` / ``dnsbl_query_name`` / ``is_ipv4``)
are unit-tested directly. ``derive_candidates`` assembles the public-facing
candidate set from four sources; ``check_one`` does the reversed-octet
A + TXT lookup for one (ip, list); ``run_sweep`` / ``check_ip_now`` persist
per-(ip, list) latch state into ``dnsbl_listing``.

IPv4 only for v1 — the major DNSBLs are IPv4-centric. IPv6 DNSBL (nibble-
reversed ``ip6.arpa``-style suffixes on the handful of lists that support
it) is a documented future enhancement; v6 candidates are skipped.

The engine never raises on a resolver error — a SERVFAIL / timeout is
recorded as ``check_error`` on the row so a flaky list can't wedge the
sweep or flip a listing off. Only a definitive NXDOMAIN means "not listed".
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dnsbl import (
    SOURCE_INTERNET_FACING,
    SOURCE_IPAM,
    SOURCE_NAT_EGRESS,
    SOURCE_PINNED,
    DNSBLList,
    DNSBLListing,
    DNSBLPinnedIP,
)
from app.models.ipam import IPAddress, NATMapping, Subnet
from app.services.ipam.classify import is_private_ip

logger = structlog.get_logger(__name__)

# Politeness delay between individual DNS queries so a full sweep doesn't
# hammer a list's resolver in a burst (some lists rate-limit aggressively).
# Jittered around this base.
_QUERY_DELAY_S = 0.05
_QUERY_JITTER_S = 0.05
_RESOLVER_TIMEOUT_S = 5.0

# Source precedence when the same IP is surfaced by multiple derivations —
# the highest-priority label is what the listing row records (purely for
# operator context in the UI).
_SOURCE_PRIORITY = {
    SOURCE_PINNED: 3,
    SOURCE_NAT_EGRESS: 2,
    SOURCE_INTERNET_FACING: 1,
    SOURCE_IPAM: 0,
}


def is_ipv4(ip: str) -> bool:
    """True iff ``ip`` (a bare address, no CIDR) is a valid IPv4 literal."""
    try:
        return isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
    except ValueError:
        return False


def reversed_octets(ip: str) -> str:
    """``1.2.3.4`` → ``4.3.2.1`` (the DNSBL query prefix). IPv4 only."""
    addr = ipaddress.IPv4Address(ip)
    return ".".join(reversed(str(addr).split(".")))


def dnsbl_query_name(ip: str, zone_suffix: str) -> str:
    """Full DNSBL query name, e.g. ``4.3.2.1.zen.spamhaus.org``."""
    return f"{reversed_octets(ip)}.{zone_suffix.strip('.').lower()}"


def _bare_ip(value: str | None) -> str | None:
    """Strip a ``/prefix`` if present; return the bare address or None."""
    if not value:
        return None
    return str(value).split("/")[0].strip() or None


@dataclass
class CheckResult:
    """Outcome of one (ip, list) reversed-octet lookup."""

    listed: bool = False
    return_codes: list[str] = field(default_factory=list)
    txt_reason: str | None = None
    error: str | None = None


async def derive_candidates(db: AsyncSession) -> dict[str, str]:
    """Assemble the public-facing IPv4 candidate set.

    Returns ``{ip: source}`` — deduped, private/reserved skipped, IPv6
    skipped. ``source`` is the highest-precedence origin per ``_SOURCE_PRIORITY``.

    Four sources:
      * IPAM public IPv4 rows (``ip_address`` where the address is not
        RFC1918 / CGNAT / link-local / loopback);
      * every IP in an ``internet_facing``-classified subnet (#75);
      * NAT / hide-NAT / PAT external (egress) addresses;
      * operator-pinned IPs (``dnsbl_pinned_ip``).
    """
    out: dict[str, str] = {}

    def _add(ip: str | None, source: str) -> None:
        ip = _bare_ip(ip)
        if not ip or not is_ipv4(ip):
            return
        # internet_facing / ipam candidates skip private space; NAT egress
        # and pinned IPs are trusted as-given (an operator pins with intent,
        # and a NAT external side is public by construction) but we still
        # drop obviously-private ones to avoid noise.
        if is_private_ip(ip):
            return
        prev = out.get(ip)
        if prev is None or _SOURCE_PRIORITY[source] > _SOURCE_PRIORITY[prev]:
            out[ip] = source

    # 1. IPAM public IPv4 rows.
    for (addr,) in (await db.execute(select(IPAddress.address))).all():
        _add(str(addr) if addr is not None else None, SOURCE_IPAM)

    # 2. IPs in internet_facing subnets.
    for (addr,) in (
        await db.execute(
            select(IPAddress.address)
            .join(Subnet, Subnet.id == IPAddress.subnet_id)
            .where(Subnet.internet_facing.is_(True))
        )
    ).all():
        _add(str(addr) if addr is not None else None, SOURCE_INTERNET_FACING)

    # 3. NAT / hide-NAT / PAT external (egress) addresses.
    for (ext_ip,) in (
        await db.execute(select(NATMapping.external_ip).where(NATMapping.external_ip.is_not(None)))
    ).all():
        _add(str(ext_ip) if ext_ip is not None else None, SOURCE_NAT_EGRESS)

    # 4. Operator-pinned IPs.
    for (pin_ip,) in (await db.execute(select(DNSBLPinnedIP.ip))).all():
        _add(str(pin_ip) if pin_ip is not None else None, SOURCE_PINNED)

    return out


def build_resolver(resolvers: list[str] | None = None) -> Any:
    """Construct ONE ``dns.asyncresolver.Resolver`` for reuse across a sweep.

    Returns ``None`` if dnspython is unavailable (``check_one`` then reports
    the ``dnspython unavailable`` error). Built once per ``run_sweep`` /
    ``check_ip_now`` and threaded into every ``check_one`` so we don't pay
    ``configure=True`` (``/etc/resolv.conf`` parse) per (ip, list) lookup.
    """
    try:
        import dns.asyncresolver  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — dnspython missing / import error
        return None
    rt = dns.asyncresolver.Resolver(configure=not resolvers)
    if resolvers:
        rt.nameservers = list(resolvers)
    rt.timeout = _RESOLVER_TIMEOUT_S
    rt.lifetime = _RESOLVER_TIMEOUT_S
    return rt


async def check_one(
    ip: str,
    list_row: DNSBLList,
    resolvers: list[str] | None = None,
    *,
    resolver: Any = None,
) -> CheckResult:
    """Reversed-octet A + TXT lookup for one (ip, list). Never raises.

    Pass ``resolver`` to reuse one pre-built resolver across a whole sweep;
    when omitted a fresh one is constructed (kept for direct/on-demand calls).
    """
    try:
        import dns.exception  # noqa: PLC0415
        import dns.resolver  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — dnspython missing / import error
        return CheckResult(error="dnspython unavailable")

    qname = dnsbl_query_name(ip, list_row.zone_suffix)

    rt = resolver if resolver is not None else build_resolver(resolvers)
    if rt is None:
        return CheckResult(error="dnspython unavailable")

    result = CheckResult()
    try:
        answer = await rt.resolve(qname, "A", raise_on_no_answer=False)
        codes = [str(rr) for rr in answer] if answer.rrset is not None else []
        if codes:
            result.listed = True
            result.return_codes = codes
    except dns.resolver.NXDOMAIN:
        # Definitive "not listed".
        return CheckResult(listed=False)
    except (dns.resolver.NoNameservers, dns.exception.Timeout) as exc:
        return CheckResult(error=f"resolver error: {exc.__class__.__name__}")
    except dns.exception.DNSException as exc:  # noqa: BLE001
        return CheckResult(error=f"dns error: {exc.__class__.__name__}")

    if result.listed:
        # Best-effort TXT for the human reason / delist URL.
        try:
            txt = await rt.resolve(qname, "TXT", raise_on_no_answer=False)
            reasons: list[str] = []
            if txt.rrset is not None:
                for rr in txt:
                    strings = getattr(rr, "strings", None)
                    if strings:
                        reasons.append(" ".join(s.decode("utf-8", "ignore") for s in strings))
                    else:
                        reasons.append(str(rr).strip('"'))
            if reasons:
                result.txt_reason = " | ".join(reasons)[:2000]
        except dns.exception.DNSException as exc:
            # Best-effort only — the A-record listing verdict already stands;
            # a missing/failed TXT just means no human-readable reason string.
            logger.debug(
                "dnsbl_txt_lookup_failed",
                ip=ip,
                qname=qname,
                zone=list_row.zone_suffix,
                error=exc.__class__.__name__,
            )
    return result


async def _apply_result(
    db: AsyncSession,
    ip: str,
    source: str,
    list_row: DNSBLList,
    res: CheckResult,
    now: datetime,
    existing: dict[tuple[str, uuid.UUID], DNSBLListing],
) -> None:
    """Upsert the ``dnsbl_listing`` row + maintain the listed/resolved latch.

    ``existing`` is the sweep-scoped ``{(ip, list_id): row}`` cache preloaded
    in one query — looked up in memory instead of a SELECT per (ip, list).
    A newly-created row is inserted back into the cache so a later pass in the
    same sweep sees it.
    """
    key = (ip, list_row.id)
    row = existing.get(key)
    if row is None:
        row = DNSBLListing(ip=ip, list_id=list_row.id)
        db.add(row)
        existing[key] = row

    row.source = source
    row.last_checked_at = now
    row.check_error = res.error

    # A resolver error leaves the prior listed-state untouched (never flip a
    # listing off on a transient failure — that would spuriously auto-resolve
    # the alert). Only a clean answer moves the latch.
    if res.error is not None:
        return

    row.return_codes = res.return_codes
    row.txt_reason = res.txt_reason

    if res.listed:
        if not row.listed:
            row.listed = True
            row.first_listed_at = now
            row.resolved_at = None
        # already listed → keep first_listed_at; refresh codes only.
    else:
        if row.listed:
            row.listed = False
            row.resolved_at = now


async def _reconcile_descoped(db: AsyncSession, candidates: dict[str, str], now: datetime) -> int:
    """Auto-resolve open listings the platform no longer monitors (#528).

    An open latch (``listed=True`` / ``resolved_at IS NULL``) whose IP has
    left the candidate set (operator unpinned it, its IPAM row was deleted,
    its subnet was un-flagged ``internet_facing``) — or whose list has been
    disabled — is resolved here (``listed=False`` + ``resolved_at``) so the
    ``ip_blocklisted`` alert auto-resolves instead of paging forever for an
    IP that's no longer swept.

    This only touches genuinely de-scoped IPs / disabled lists; it never
    reaches a still-candidate IP on an enabled list, so a transient DNS
    error in the active sweep (which leaves the latch untouched) can't be
    mistaken for a delist here.
    """
    open_rows = (
        await db.execute(
            select(DNSBLListing, DNSBLList.enabled)
            .join(DNSBLList, DNSBLList.id == DNSBLListing.list_id)
            .where(DNSBLListing.listed.is_(True), DNSBLListing.resolved_at.is_(None))
        )
    ).all()
    resolved = 0
    for row, list_enabled in open_rows:
        if str(row.ip) in candidates and list_enabled:
            continue  # still monitored on an enabled list — leave the latch.
        row.listed = False
        row.resolved_at = now
        resolved += 1
    return resolved


async def run_sweep(db: AsyncSession, *, resolvers: list[str] | None = None) -> dict[str, int]:
    """Full candidate × enabled-list sweep. Idempotent. Returns counters."""
    enabled_lists = (
        (await db.execute(select(DNSBLList).where(DNSBLList.enabled.is_(True)))).scalars().all()
    )
    candidates = await derive_candidates(db)
    now = datetime.now(UTC)
    counters = {
        "candidates": len(candidates),
        "lists": len(enabled_lists),
        "checks": 0,
        "listed": 0,
        "errors": 0,
        "resolved": 0,
    }

    # Auto-resolve latches for IPs that left the candidate set / lists that
    # got disabled FIRST — this must run even when no enabled lists remain
    # (e.g. the operator just disabled the only list), so it precedes the
    # early return below.
    counters["resolved"] = await _reconcile_descoped(db, candidates, now)

    if not enabled_lists:
        await db.commit()
        return counters

    # Preload existing listing rows for the candidate set in one query, then
    # look them up in memory in _apply_result (no SELECT per (ip, list)).
    existing: dict[tuple[str, uuid.UUID], DNSBLListing] = {}
    if candidates:
        rows = (
            (
                await db.execute(
                    select(DNSBLListing).where(DNSBLListing.ip.in_(list(candidates.keys())))
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            existing[(str(r.ip), r.list_id)] = r

    # One resolver for the whole sweep — reused across every lookup. The
    # per-query jitter/throttle below keeps us rate-limit friendly.
    resolver = build_resolver(resolvers)

    for ip, source in candidates.items():
        for list_row in enabled_lists:
            res = await check_one(ip, list_row, resolvers, resolver=resolver)
            await _apply_result(db, ip, source, list_row, res, now, existing)
            counters["checks"] += 1
            if res.error is not None:
                counters["errors"] += 1
            elif res.listed:
                counters["listed"] += 1
            await asyncio.sleep(_QUERY_DELAY_S + random.uniform(0, _QUERY_JITTER_S))
        # Commit per-IP so a long sweep persists progress incrementally.
        await db.commit()
    # Flush the reconcile changes when there were no candidate iterations to
    # trigger the per-IP commit above.
    await db.commit()
    return counters


async def check_ip_now(
    db: AsyncSession, ip: str, *, resolvers: list[str] | None = None
) -> dict[str, object]:
    """On-demand single-IP check across every enabled list. Persists state.

    Used by the IP-detail-modal "Check now" button. Determines the source
    from the derived candidate set (falling back to ``pinned`` when the IP
    isn't auto-derived — an operator explicitly asked about it)."""
    ip = _bare_ip(ip) or ip
    if not is_ipv4(ip):
        return {"ip": ip, "error": "IPv4 only (v1)", "checked": 0}

    enabled_lists = (
        (await db.execute(select(DNSBLList).where(DNSBLList.enabled.is_(True)))).scalars().all()
    )
    candidates = await derive_candidates(db)
    source = candidates.get(ip, SOURCE_PINNED)
    now = datetime.now(UTC)

    # Preload this IP's existing listing rows in one query (mirrors run_sweep).
    existing: dict[tuple[str, uuid.UUID], DNSBLListing] = {}
    for r in (await db.execute(select(DNSBLListing).where(DNSBLListing.ip == ip))).scalars().all():
        existing[(str(r.ip), r.list_id)] = r

    resolver = build_resolver(resolvers)
    checked = 0
    listed = 0
    for list_row in enabled_lists:
        res = await check_one(ip, list_row, resolvers, resolver=resolver)
        await _apply_result(db, ip, source, list_row, res, now, existing)
        checked += 1
        if res.listed and res.error is None:
            listed += 1
    await db.commit()
    return {"ip": ip, "checked": checked, "listed": listed, "source": source}
