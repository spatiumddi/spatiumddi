"""Phase 2 shadow-query comparison — ask both sides the same questions.

Parity (:mod:`app.services.cutover.parity`) proves the two *configurations*
agree. That is necessary and not sufficient: a zone can be byte-identical in
the database and still answer differently on the wire, because of a view match
that fires on the control plane's source address, a stale zone file an agent
never reloaded, a CNAME the source resolves through and the target doesn't, or
a record served from a forwarder rather than from the zone.

So before the switch we replay real questions at both sides and compare the
answers. Names come from the BIND9 query log (:class:`DNSQueryLogEntry`) when
there is any — those are queries clients actually asked in the last 24 hours,
which makes this a shadow run rather than a second look at our own rows — and
fall back to the zone's ``DNSRecord`` names when the log is empty (query
logging off, or the managed side not yet receiving traffic). ``sample_source``
on the report says which, because the two are not equally strong evidence.

Three things this module is careful about:

* **Queries go to an IP literal, never a hostname.** Every low-level dnspython
  entry point resolves the nameserver itself and raises a bare, message-less
  ``ValueError`` for anything that is not an address. That is exactly what made
  the #61 drift report fail 100% of the time against every hostname-addressed
  server (#734), reporting ``""`` as the reason. Hostnames are resolved here,
  off the event loop, and a failure becomes a per-target warning rather than
  taking down the whole run.
* **Recursion is disabled on the wire (RD=0).** Both sides are supposed to be
  authoritative for the zone; letting either recurse would allow a cached
  answer from somewhere else to stand in for the server under test.
* **Nothing here raises for a single bad answer.** A timeout against one target
  is data about that target, not a failed comparison.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import dns.asyncresolver
import dns.exception
import dns.resolver
import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dns import CLOUD_DNS_DRIVERS
from app.drivers.dns._axfr import resolve_server_address
from app.models.dns import DNSRecord, DNSServer, DNSZone
from app.models.logs import DNSQueryLogEntry
from app.services.cutover.canonical import ShadowReport, ShadowSample

logger = structlog.get_logger(__name__)

#: Per-query wall clock. Short on purpose — an authoritative server on the LAN
#: answers in single-digit milliseconds, and a whole-zone sample must not turn
#: one dead target into a minutes-long request.
QUERY_TIMEOUT_SECONDS = 3.0

#: In-flight query ceiling across the entire run. ``sample_size`` × (1 source +
#: N targets) queries are scheduled at once; this is what keeps that from
#: becoming a self-inflicted flood at 200 samples.
MAX_CONCURRENT_QUERIES = 8

MIN_SAMPLE_SIZE = 1
MAX_SAMPLE_SIZE = 200

#: How far back in the query log to look for names. Matches the 24 h retention
#: ``app.tasks.prune_log_entries`` enforces, so this never asks for rows that
#: cannot exist.
QUERY_LOG_WINDOW_HOURS = 24

#: Types worth replaying — the wire-queryable subset of ``VALID_RECORD_TYPES``.
#: Excludes ANY / AXFR / IXFR (meta-queries whose answers legitimately differ
#: between implementations), the DNSSEC signing artefacts the signing daemon
#: mints and rotates itself, and PowerDNS's ALIAS / LUA, which are authoring
#: pseudo-types that never appear as a qtype on the wire.
SAMPLEABLE_QTYPES: frozenset[str] = frozenset(
    {
        "A",
        "AAAA",
        "CAA",
        "CNAME",
        "DNAME",
        "HTTPS",
        "LOC",
        "MX",
        "NAPTR",
        "NS",
        "PTR",
        "SOA",
        "SRV",
        "SSHFP",
        "SVCB",
        "TLSA",
        "TXT",
    }
)


@dataclass
class _QueryOutcome:
    """One server's reply to one question.

    ``error`` is set only for a failure to *get* an answer (timeout, refused,
    unreachable). NXDOMAIN and an empty answer section are both legitimate
    answers and come back as ``answers == []`` with no error — otherwise a name
    that is correctly absent from both sides would read as a broken comparison.
    """

    answers: list[str] = field(default_factory=list)
    error: str | None = None


def _clamp_sample_size(sample_size: int) -> int:
    return max(MIN_SAMPLE_SIZE, min(MAX_SAMPLE_SIZE, int(sample_size)))


def _zone_label(zone: DNSZone) -> str:
    """The zone name without its trailing dot, lower-cased."""
    return zone.name.strip().rstrip(".").lower()


def _server_label(server: DNSServer) -> str:
    """Human label used as the key in ``ShadowSample.target_answers``."""
    return f"{server.name} ({server.host})"


#: Types whose RDATA is entirely case- and trailing-dot-insensitive, so folding
#: both is safe. These are the name-valued types (DNS names compare
#: case-insensitively per RFC 4343, and implementations disagree about rendering
#: the root dot on a target) plus the address types, where hex case in an IPv6
#: literal carries no meaning.
#:
#: Everything NOT listed here is compared verbatim. TXT is the one that matters:
#: DKIM public keys, `google-site-verification=` tokens and the like are
#: case-sensitive payloads, and folding them would let the shadow run report a
#: "match" between two records a resolver would treat as different. CAA tags,
#: NAPTR flags and regexps are case-bearing for the same reason.
_CASE_FOLDED_QTYPES: frozenset[str] = frozenset(
    {"A", "AAAA", "CNAME", "NS", "PTR", "MX", "SRV", "SOA"}
)


def normalise_answers(rendered: Iterable[str], qtype: str) -> list[str]:
    """Canonical form for comparing two answer sets of type ``qtype``.

    Always sorted and de-duplicated: RRset order is not meaningful on the wire
    (and round-robin makes it actively unstable), so comparing sorted sets is
    the only comparison that does not produce false mismatches.

    Case and the trailing dot are folded **only** for the types in
    :data:`_CASE_FOLDED_QTYPES`. For anything else the RDATA is compared as the
    server rendered it — see that constant for why folding a TXT record would be
    a correctness bug rather than a convenience.
    """
    values = (r.strip() for r in rendered)
    if qtype.strip().upper() in _CASE_FOLDED_QTYPES:
        return sorted({v.rstrip(".").lower() for v in values if v})
    return sorted({v for v in values if v})


async def _resolve_to_ip(host: str, port: int, *, what: str) -> str:
    """Return the first IP literal for ``host``.

    ``resolve_server_address`` is a blocking ``getaddrinfo`` call, so it runs
    off the event loop. It raises ``RuntimeError`` with an operator-readable
    message when the name does not resolve; callers turn that into a warning.
    """
    addrs = await asyncio.to_thread(resolve_server_address, host, port, what=what)
    return addrs[0]


async def _query(ip: str, qname: str, qtype: str, timeout: float) -> _QueryOutcome:
    """Ask ``ip`` one question, authoritatively, with its own timeout.

    A fresh resolver per call so a slow server cannot poison the others. The
    exception ladder mirrors ``app.api.v1.dns_tools._query_one`` — NXDOMAIN
    first (it subclasses ``DNSException``), then timeout, then any other DNS
    error, then the socket-level failures dnspython lets through as ``OSError``.
    """
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [ip]
    resolver.timeout = timeout
    resolver.lifetime = timeout
    # RD=0. Both sides should be authoritative for this zone; a recursive
    # answer would let a cache answer for the server we are trying to test.
    resolver.flags = 0

    try:
        answer = await resolver.resolve(qname, qtype, raise_on_no_answer=False)
    except dns.resolver.NXDOMAIN:
        return _QueryOutcome()
    except dns.exception.Timeout:
        return _QueryOutcome(error=f"timeout after {timeout:.1f}s")
    except dns.exception.DNSException as exc:
        return _QueryOutcome(error=str(exc) or exc.__class__.__name__)
    except OSError as exc:
        return _QueryOutcome(error=f"network error: {exc}")

    rendered = [str(rr) for rr in answer] if answer.rrset else []
    return _QueryOutcome(answers=normalise_answers(rendered, qtype))


async def _sample_from_query_log(
    db: AsyncSession, zone: DNSZone, limit: int
) -> list[tuple[str, str]]:
    """Recently-asked ``(qname, qtype)`` pairs inside ``zone``, newest first.

    Scoped to the servers in the zone's own group: a zone name can exist in
    more than one group (and more than one view), and replaying another group's
    traffic would compare the wrong population of names.
    """
    label = _zone_label(zone)
    cutoff = datetime.now(UTC) - timedelta(hours=QUERY_LOG_WINDOW_HOURS)
    lowered = func.lower(DNSQueryLogEntry.qname)
    group_server_ids = select(DNSServer.id).where(DNSServer.group_id == zone.group_id)

    rows = (
        await db.execute(
            select(DNSQueryLogEntry.qname, DNSQueryLogEntry.qtype)
            .where(
                DNSQueryLogEntry.server_id.in_(group_server_ids),
                DNSQueryLogEntry.ts >= cutoff,
                DNSQueryLogEntry.qname.is_not(None),
                or_(
                    lowered == label,
                    lowered == f"{label}.",
                    # autoescape: a DNS label may legally contain ``_``
                    # (``_ldap._tcp``), which is a LIKE wildcard.
                    lowered.endswith(f".{label}", autoescape=True),
                    lowered.endswith(f".{label}.", autoescape=True),
                ),
            )
            # Over-fetch: the newest rows are dominated by whatever is being
            # queried hardest right now, and we want ``limit`` DISTINCT names.
            .order_by(DNSQueryLogEntry.ts.desc())
            .limit(limit * 20)
        )
    ).all()

    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for qname, qtype in rows:
        name = (qname or "").strip().rstrip(".").lower()
        rtype = (qtype or "").strip().upper()
        if not name or rtype not in SAMPLEABLE_QTYPES:
            continue
        key = (name, rtype)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


async def _sample_from_records(
    db: AsyncSession, zone: DNSZone, limit: int
) -> list[tuple[str, str]]:
    """Fallback name source: the zone's own records, as FQDN + type pairs."""
    label = _zone_label(zone)
    records = list(
        (
            await db.execute(
                select(DNSRecord)
                .where(DNSRecord.zone_id == zone.id)
                .order_by(DNSRecord.name, DNSRecord.record_type)
                .limit(limit * 10)
            )
        )
        .scalars()
        .all()
    )

    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        rtype = (rec.record_type or "").strip().upper()
        if rtype not in SAMPLEABLE_QTYPES:
            continue
        rel = (rec.name or "@").strip().rstrip(".").lower()
        name = label if rel in ("", "@") else f"{rel}.{label}"
        key = (name, rtype)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


async def _collect_targets(
    db: AsyncSession, zone: DNSZone, source_server: DNSServer, report: ShadowReport
) -> list[tuple[str, str]]:
    """Resolve the managed side of the comparison to ``(label, ip)`` pairs.

    Skips the source server itself (a plan can legitimately point at a group
    the Windows server was also registered in) and the cloud-hosted drivers,
    whose ``host`` is a display label rather than a resolver we can query —
    their zones are read through a provider API, not off the wire.
    """
    servers = list(
        (
            await db.execute(
                select(DNSServer)
                .where(DNSServer.group_id == zone.group_id, DNSServer.is_enabled.is_(True))
                .order_by(DNSServer.is_primary.desc(), DNSServer.name)
            )
        )
        .scalars()
        .all()
    )

    targets: list[tuple[str, str]] = []
    for srv in servers:
        if srv.id == source_server.id:
            continue
        if srv.driver in CLOUD_DNS_DRIVERS:
            report.warnings.append(
                f"Skipped {srv.name!r}: the {srv.driver!r} driver is a hosted provider "
                "reached over its API, so there is no address to send a query to."
            )
            continue
        label = _server_label(srv)
        try:
            ip = await _resolve_to_ip(srv.host, srv.port or 53, what=f"Shadow query of {zone.name}")
        except RuntimeError as exc:
            report.warnings.append(f"Skipped {srv.name!r}: {exc}")
            continue
        targets.append((label, ip))
        report.targets.append(label)
    return targets


def _score(sample: ShadowSample, report: ShadowReport) -> None:
    """Fold one finished sample into the report's counters."""
    if sample.verdict == "match":
        report.matched += 1
    elif sample.verdict == "mismatch":
        report.mismatched += 1
    else:
        report.errors += 1


async def run_shadow_comparison(
    db: AsyncSession,
    *,
    source_server: DNSServer,
    zone: DNSZone,
    sample_size: int = 25,
) -> ShadowReport:
    """Replay a sample of real queries against Windows and the managed group.

    Returns a :class:`ShadowReport` in every reachable-or-not case. The only
    ways to get an empty report are: the source host does not resolve, no
    queryable target exists in the zone's group, or there is nothing to ask —
    each of which lands as a warning saying so, because a silent zero would
    read as "everything matched".
    """
    limit = _clamp_sample_size(sample_size)
    report = ShadowReport(zone_name=zone.name, source=_server_label(source_server))

    # Answers here are read with recursion disabled, and a view-scoped zone is
    # answered according to whichever view matches OUR source address — the
    # same caveat #61's drift report carries, and for the same reason.
    if zone.view_id is not None:
        report.warnings.append(
            "This zone belongs to a DNS view. Servers answer with whichever view "
            "matches the control plane's source address, so differences below may "
            "reflect a different view rather than a real difference."
        )

    try:
        source_ip = await _resolve_to_ip(
            source_server.host, source_server.port or 53, what=f"Shadow query of {zone.name}"
        )
    except RuntimeError as exc:
        report.warnings.append(f"Source server {source_server.name!r} is not queryable: {exc}")
        return report

    targets = await _collect_targets(db, zone, source_server, report)
    if not targets:
        report.warnings.append(
            "No queryable target server in this zone's group, so there was nothing "
            "to compare against. Add or enable an agent-managed server in the group."
        )
        return report

    samples = await _sample_from_query_log(db, zone, limit)
    if samples:
        report.sample_source = "query_log"
    else:
        samples = await _sample_from_records(db, zone, limit)
        report.sample_source = "zone_records"
        report.warnings.append(
            "No recent query-log entries for this zone, so names were taken from its "
            "records instead. Enable query logging on the managed servers and re-run "
            "during the parallel window for a comparison against real traffic."
        )
    if not samples:
        report.warnings.append("This zone has no records and no recent queries to sample.")
        return report

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)

    async def _guarded(ip: str, qname: str, qtype: str) -> _QueryOutcome:
        async with semaphore:
            return await _query(ip, qname, qtype, QUERY_TIMEOUT_SECONDS)

    async def _one_sample(qname: str, qtype: str) -> ShadowSample:
        sample = ShadowSample(name=qname, qtype=qtype)
        # Trailing dot: with ``configure=False`` there is no search list, but
        # an absolute name removes any doubt about ndots handling.
        absolute = f"{qname}."
        outcomes = await asyncio.gather(
            _guarded(source_ip, absolute, qtype),
            *(_guarded(ip, absolute, qtype) for _, ip in targets),
        )

        source_outcome = outcomes[0]
        if source_outcome.error:
            sample.verdict = "source_error"
            sample.error = source_outcome.error
            return sample

        sample.source_answers = source_outcome.answers
        target_errors: list[str] = []
        mismatched = False
        for (label, _ip), outcome in zip(targets, outcomes[1:], strict=True):
            if outcome.error:
                target_errors.append(f"{label}: {outcome.error}")
                continue
            sample.target_answers[label] = outcome.answers
            if outcome.answers != source_outcome.answers:
                mismatched = True

        if target_errors:
            # A target we could not reach outranks a mismatch we did see: the
            # comparison is incomplete, and reporting it as a clean mismatch
            # would understate how much is still unknown.
            sample.verdict = "target_error"
            sample.error = "; ".join(target_errors)
        elif mismatched:
            sample.verdict = "mismatch"
        else:
            sample.verdict = "match"
        return sample

    report.samples = list(await asyncio.gather(*(_one_sample(n, t) for n, t in samples)))
    report.sampled = len(report.samples)
    for sample in report.samples:
        _score(sample, report)

    logger.info(
        "cutover.shadow.completed",
        zone=zone.name,
        source=source_server.host,
        targets=len(targets),
        sample_source=report.sample_source,
        sampled=report.sampled,
        matched=report.matched,
        mismatched=report.mismatched,
        errors=report.errors,
    )
    return report


__all__ = [
    "MAX_CONCURRENT_QUERIES",
    "MAX_SAMPLE_SIZE",
    "MIN_SAMPLE_SIZE",
    "QUERY_LOG_WINDOW_HOURS",
    "QUERY_TIMEOUT_SECONDS",
    "SAMPLEABLE_QTYPES",
    "normalise_answers",
    "run_shadow_comparison",
]
