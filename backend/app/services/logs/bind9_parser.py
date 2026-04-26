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

# The interesting body. ``client`` form (most common) or ``view ...:
# query:`` form (BIND with views).
#
# Examples:
#   client @0x7f8b1c001234 192.0.2.5#54321 (example.com): query: example.com IN A +E(0)K (10.0.0.1)
#   client @0x... 2001:db8::1#34567 (foo.bar): query: foo.bar IN AAAA + (2001:db8::dead)
_QUERY_RE: Final = re.compile(
    r"client\s+"
    # Opaque pointer like ``@0x7f8b1c001234``. We accept any
    # non-whitespace run so logs from builds emitting different
    # pointer formats (or test fixtures using `@0x...`) still parse.
    r"(?:@\S+\s+)?"
    r"(?P<client_ip>\S+?)"
    r"#(?P<client_port>\d+)"
    r"(?:\s+\((?P<echo>[^)]*)\))?"
    # View name appears as ``view <name>`` (no parens) or
    # ``(view <name>)`` depending on BIND build / config. Tolerate both.
    r"(?:\s*\(view\s+(?P<view_paren>[^)]+)\))?"
    r"(?:\s+view\s+(?P<view_bare>\S+))?"
    r":\s+query:\s+"
    r"(?P<qname>\S+)\s+(?P<qclass>\S+)\s+(?P<qtype>\S+)"
    r"(?:\s+(?P<flags>\S+))?"
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


def parse_query_line(line: str, *, fallback_ts: datetime | None = None) -> ParsedQueryLine | None:
    """Parse one BIND9 query log line.

    Returns ``None`` only when the line is empty / whitespace-only.
    Lines that the regex can't match still come back as
    ``ParsedQueryLine`` with most fields ``None`` and the raw text
    preserved, so the UI can still surface them.

    ``fallback_ts`` is used when the line has no leading timestamp
    (e.g. ``print-time no`` in named.conf). The shipper passes the
    receive-time so we don't drop the line.
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None

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

    qm = _QUERY_RE.search(rest)
    if not qm:
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

    try:
        client_port = int(qm.group("client_port"))
    except (TypeError, ValueError):
        client_port = None

    return ParsedQueryLine(
        ts=ts,
        client_ip=qm.group("client_ip") or None,
        client_port=client_port,
        qname=qm.group("qname") or None,
        qclass=qm.group("qclass") or None,
        qtype=qm.group("qtype") or None,
        flags=qm.group("flags") or None,
        view=qm.group("view_paren") or qm.group("view_bare") or None,
        raw=line,
    )


__all__ = ["ParsedQueryLine", "parse_query_line"]
