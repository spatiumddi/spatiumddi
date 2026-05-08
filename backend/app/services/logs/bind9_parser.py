"""BIND9 query log line parser.

A typical BIND9 query log line looks like::

    25-Apr-2026 16:30:01.123 client @0x7f... 192.0.2.5#54321 (example.com): query: example.com IN A +E(0)K (10.0.0.1)

The leading timestamp is present when the channel sets
``print-time yes`` (we always render it that way — see
``backend/app/drivers/dns/templates/bind9/named.conf.j2``). The
``client @0x... <ip>#<port>`` block carries the resolver address and
the ephemeral source port; ``(<question>):`` is BIND's polite echo
of the query name. The ``query:`` body itself follows: ``<qname>
<qclass> <qtype> <flags>``. The trailing ``(<server>)`` is the
listening interface BIND received the query on (often the host's
LAN address) and is mostly noise for the UI.

This parser is deliberately liberal — every regex group is optional
because BIND configurations vary (no ``print-time``, no
``print-category``, IPv6 clients, view names, etc). When a field
can't be extracted we leave it as ``None`` and the caller stores the
raw line untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

# ── Regex bits ───────────────────────────────────────────────────────

# BIND9's default time format with print-time yes:
#   ``25-Apr-2026 16:30:01.123``
_BIND_TS_RE: Final = re.compile(
    r"^(?P<ts>\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+"
)

# ISO-8601 fallback (some operators reconfigure BIND to emit iso):
#   ``2026-04-25T16:30:01.123Z`` or ``2026-04-25 16:30:01.123``
_ISO_TS_RE: Final = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+\-]\d{2}:?\d{2})?)\s+"
)

# Optional ``[severity]``-style category prefix BIND prepends when
# ``print-severity`` / ``print-category`` are on. We tolerate both.
_CAT_SEV_RE: Final = re.compile(r"^(?:queries:\s+)?(?:info:\s+)?")

# BIND9 query lines are split on the hard ``: query: `` separator
# rather than matched by one big regex. The combined pattern (client
# + optional view block + optional bare view + qname/qclass/qtype +
# optional flags) had three independent ``\s+``-anchored optional
# groups that gave the regex engine room to try multiple alignments
# of whitespace runs on adversarial input — see CodeQL
# py/polynomial-redos alert #16. Splitting on ``: query: `` first
# disambiguates the structure: nothing on either side can contain
# the literal ``: query: ``, and each side gets a small, linear
# regex.
#
# Examples:
#   client @0x7f8b1c001234 192.0.2.5#54321 (example.com): query: example.com IN A +E(0)K (10.0.0.1)
#   client @0x... 2001:db8::1#34567 (foo.bar): query: foo.bar IN AAAA + (2001:db8::dead)
_QUERY_SEP_RE: Final = re.compile(r":\s+query:\s+", re.IGNORECASE)

# Head: everything between ``client`` and the ``: query: `` separator.
# Captures client IP + port up front; the parenthesised echo / view
# block and the bare ``view <name>`` form are extracted with a
# follow-up regex against the *remainder* — no two ``\s+``-anchored
# optionals competing for the same whitespace run.
_HEAD_RE: Final = re.compile(
    r"^client\s+" r"(?:@\S+\s+)?" r"(?P<client_ip>\S+?)" r"#(?P<client_port>\d+)" r"(?P<rest>.*)$",
    re.DOTALL,
)

# Pulls a view name out of the head's remainder. Either
# ``(view <name>)`` or bare ``view <name>``. Run with ``search`` so
# leading whitespace / parenthesised echo blocks are skipped over.
#
# CodeQL alert #18 (polynomial ReDoS): the previous shape was
# ``\(\s*view\s+(?P<view_paren>[^)]+?)\s*\)`` — and even the
# intermediate ``\(view\s+(?P<view_paren>[^)]*)\)`` is quadratic
# because ``\s+`` and ``[^)]*`` both consume whitespace and the
# engine enumerates every split between them when the trailing
# ``\)`` fails to match. Real BIND9 view names are single tokens
# (DNS-name-like — no embedded whitespace), so the safe shape is
# to constrain the inner capture to non-whitespace-non-paren
# (``[^)\s]+``). ``\s+`` matches whitespace exclusively, the
# capture matches non-whitespace exclusively, and the optional
# trailing ``\s*`` before ``\)`` doesn't overlap with the capture
# either — every segment has a disjoint character class, so the
# total time is linear in the input length.
_VIEW_RE: Final = re.compile(
    r"\(view\s+(?P<view_paren>[^)\s]+)\s*\)" r"|" r"\bview\s+(?P<view_bare>\S+)",
)

# Body: ``<qname> <qclass> <qtype> [<flags>]`` — what follows
# ``: query: ``. Anchored at the start so we don't search; bounded
# by ``\S+`` runs separated by single ``\s+`` matches that can't
# overlap thanks to the anchor.
_BODY_RE: Final = re.compile(
    r"^(?P<qname>\S+)\s+(?P<qclass>\S+)\s+(?P<qtype>\S+)" r"(?:\s+(?P<flags>\S+))?",
)

_MONTH_MAP: Final[dict[str, int]] = {
    m: i + 1
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
}


@dataclass(frozen=True)
class ParsedQueryLine:
    ts: datetime
    client_ip: str | None
    client_port: int | None
    qname: str | None
    qclass: str | None
    qtype: str | None
    flags: str | None
    view: str | None
    raw: str


def _parse_bind_ts(value: str) -> datetime | None:
    """``25-Apr-2026 16:30:01.123`` → tz-aware UTC datetime.

    BIND9 doesn't stamp a timezone; we treat it as UTC since the
    agent and named are running in the same container and we always
    set the container TZ to UTC.
    """
    try:
        date_part, time_part = value.split(maxsplit=1)
        d, mon_name, y = date_part.split("-")
        mon = _MONTH_MAP.get(mon_name)
        if mon is None:
            return None
        if "." in time_part:
            time_main, frac = time_part.split(".", 1)
            micro = int(frac.ljust(6, "0")[:6])
        else:
            time_main, micro = time_part, 0
        h, m, s = (int(x) for x in time_main.split(":"))
        return datetime(int(y), mon, int(d), h, m, s, micro, tzinfo=UTC)
    except (ValueError, KeyError):
        return None


def _parse_iso_ts(value: str) -> datetime | None:
    try:
        v = value.replace("Z", "+00:00")
        if " " in v and "T" not in v:
            v = v.replace(" ", "T", 1)
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


_MAX_LINE_LEN: Final = 4096

# CHAOS-class server-identification probes (id.server / version.bind /
# version.server / hostname.bind / authors.bind). Monitoring tools
# poll these every few seconds against any DNS server they discover —
# pure noise in the operator-facing /logs UI. Drop at parse time so
# they don't even hit the DB. Match lower-cased and trailing-dot-
# stripped so case / FQDN variants ($qname or $qname.) are caught.
_CHAOS_PROBE_QNAMES: Final[frozenset[str]] = frozenset(
    {
        "id.server",
        "version.bind",
        "version.server",
        "hostname.bind",
        "authors.bind",
    }
)


def parse_query_line(line: str, *, fallback_ts: datetime | None = None) -> ParsedQueryLine | None:
    """Parse one BIND9 query log line.

    Returns ``None`` only when the line is empty / whitespace-only.
    Lines that the regex can't match still come back as
    ``ParsedQueryLine`` with most fields ``None`` and the raw text
    preserved, so the UI can still surface them.

    ``fallback_ts`` is used when the line has no leading timestamp
    (e.g. ``print-time no`` in named.conf). The shipper passes the
    receive-time so we don't drop the line.

    Lines are truncated to ``_MAX_LINE_LEN`` before regex matching to
    bound the cost of the polynomial-backtracking patterns
    (``\\s+`` repetitions + optional view groups). A real BIND9
    query line is bounded by the qname (≤ 255 chars per RFC 1035)
    plus the timestamp / client / view metadata, so 4 KiB is well
    above any legitimate line. Truncation preserves the raw text
    on the resulting ParsedQueryLine for forensic inspection.
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    if len(line) > _MAX_LINE_LEN:
        line = line[:_MAX_LINE_LEN]

    ts: datetime | None = None
    rest = line

    m = _BIND_TS_RE.match(rest)
    if m:
        ts = _parse_bind_ts(m.group("ts"))
        rest = rest[m.end() :]
    else:
        m_iso = _ISO_TS_RE.match(rest)
        if m_iso:
            ts = _parse_iso_ts(m_iso.group("ts"))
            rest = rest[m_iso.end() :]

    if ts is None:
        ts = fallback_ts or datetime.now(UTC)

    rest = _CAT_SEV_RE.sub("", rest, count=1)

    # Split on the hard ``: query: `` separator so each side gets a
    # small, linear regex. The split itself is bounded — at most one
    # cut, and ``: query: `` can't appear inside a qname or view name.
    parts = _QUERY_SEP_RE.split(rest, maxsplit=1)
    if len(parts) != 2:
        return ParsedQueryLine(
            ts=ts,
            client_ip=None,
            client_port=None,
            qname=None,
            qclass=None,
            qtype=None,
            flags=None,
            view=None,
            raw=line,
        )
    head, body = parts

    head_m = _HEAD_RE.search(head)
    body_m = _BODY_RE.match(body)
    if head_m is None or body_m is None:
        return ParsedQueryLine(
            ts=ts,
            client_ip=None,
            client_port=None,
            qname=None,
            qclass=None,
            qtype=None,
            flags=None,
            view=None,
            raw=line,
        )

    # Strip the trailing ``)`` parenthesised echo (e.g. ``(example.com)``)
    # before scanning for a view marker so the search bound is short.
    remainder = head_m.group("rest") or ""
    view: str | None = None
    vm = _VIEW_RE.search(remainder)
    if vm:
        view = (vm.group("view_paren") or vm.group("view_bare") or "").strip() or None

    try:
        client_port = int(head_m.group("client_port"))
    except (TypeError, ValueError):
        client_port = None

    qname = body_m.group("qname") or None
    # Drop CHAOS-class probes — see ``_CHAOS_PROBE_QNAMES`` above.
    if qname and qname.rstrip(".").lower() in _CHAOS_PROBE_QNAMES:
        return None

    return ParsedQueryLine(
        ts=ts,
        client_ip=head_m.group("client_ip") or None,
        client_port=client_port,
        qname=qname,
        qclass=body_m.group("qclass") or None,
        qtype=body_m.group("qtype") or None,
        flags=body_m.group("flags") or None,
        view=view,
        raw=line,
    )


__all__ = ["ParsedQueryLine", "parse_query_line"]
