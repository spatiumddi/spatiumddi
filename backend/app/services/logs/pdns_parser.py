"""PowerDNS Authoritative query-log line parser.

PowerDNS writes one stderr line per incoming query when both
``log-dns-queries=yes`` and ``log-dns-details=yes`` are set in
``pdns.conf``. The agent's stderr capture redirects that output
into ``/var/log/pdns/pdns.log`` and the QueryLogShipper tails the
file. This parser converts each line into the same
:class:`~app.services.logs.bind9_parser.ParsedQueryLine` shape so
the ingest endpoint can dispatch by ``server.driver`` without the
storage layer caring which DNS daemon emitted the line.

Sample line shapes pdns 4.9 emits::

    May 08 02:11:22 Remote 192.0.2.5:54321 wants 'www.example.com|A', do = 0, bufsize = 4096: 1 RR(s)
    May 08 02:11:22 Remote 127.0.0.1:54321 wants 'foo.example.com|AAAA', do = 1, bufsize = 4096: packetcache HIT

PowerDNS does not stamp a year on its timestamps (it uses syslog-
style ``Month DD HH:MM:SS`` derived from the C runtime). We treat
the line as occurring in the current UTC year — if a line crosses
into a new year while the agent is mid-batch, we may stamp it with
the wrong year by ~1 day. That's the same approximation the syslog
collector ecosystem makes; for query logs (24h retention) it's
inconsequential.

Lines that the regex can't match still come back as a
``ParsedQueryLine`` with most fields ``None`` and the raw text
preserved, so the operator can still see "something happened" in
the UI.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

from app.services.logs.bind9_parser import ParsedQueryLine

# ── Regex bits ───────────────────────────────────────────────────────

# pdns timestamp prefix: ``May 08 02:11:22`` or ``Apr  5 02:11:22``
# (single-digit days are right-padded with one extra space). No year.
_PDNS_TS_RE: Final = re.compile(
    r"^(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+" r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\s+"
)

# Body: ``Remote <ip>[:<port>] wants '<qname>|<qtype>'``
# - IPv6 clients are rendered as ``[2001:db8::1]:54321`` (some pdns
#   builds) or as bare ``2001:db8::1`` (others).
# - The ``:port`` suffix is omitted by pdns 4.9 in the basic log
#   format, so it's optional.
_PDNS_QUERY_RE: Final = re.compile(
    r"Remote\s+"
    r"(?:\[(?P<v6>[^\]]+)\]|(?P<v4>[0-9a-fA-F.:]+?))"
    r"(?::(?P<port>\d+))?"
    r"\s+wants\s+'(?P<qname>[^|']+)\|(?P<qtype>[^']+)'"
)

_MONTH_MAP: Final[dict[str, int]] = {
    m: i + 1
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
}

_MAX_LINE_LEN: Final = 4096

# CHAOS-class server-identification probes (id.server / version.bind /
# version.server / hostname.bind / authors.bind). Monitoring tools
# poll these every few seconds against any DNS server they discover,
# so an idle homelab daemon's query log is mostly noise from them.
# We never want them in the operator-facing /logs UI; drop at parse
# time so they don't even hit the DB. The names are case-insensitive
# in DNS but PowerDNS preserves whatever the client sent — match
# lower-cased so a client probing ``ID.SERVER`` is also caught.
_CHAOS_PROBE_QNAMES: Final[frozenset[str]] = frozenset(
    {
        "id.server",
        "version.bind",
        "version.server",
        "hostname.bind",
        "authors.bind",
    }
)


def _parse_pdns_ts(
    mon_name: str,
    day_str: str,
    h_str: str,
    m_str: str,
    s_str: str,
    *,
    now: datetime,
) -> datetime | None:
    """``May 08 02:11:22`` → tz-aware UTC datetime stamped with the
    current year. The agent and pdns share the container's UTC TZ.
    """
    mon = _MONTH_MAP.get(mon_name.title())
    if mon is None:
        return None
    try:
        return datetime(now.year, mon, int(day_str), int(h_str), int(m_str), int(s_str), tzinfo=UTC)
    except ValueError:
        return None


def parse_query_line(line: str, *, fallback_ts: datetime | None = None) -> ParsedQueryLine | None:
    """Parse one PowerDNS query log line into the shared
    :class:`ParsedQueryLine` shape.

    Returns ``None`` only when the line is empty / whitespace-only.
    Lines that don't match the ``Remote ... wants '...'`` shape
    (start-up banners, "ready to distribute questions", etc.) come
    back with ``qname`` / ``qtype`` ``None`` so the storage layer
    skips them at insert time. Lines longer than ``_MAX_LINE_LEN``
    are truncated to bound regex cost.
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    if len(line) > _MAX_LINE_LEN:
        line = line[:_MAX_LINE_LEN]

    now = fallback_ts or datetime.now(UTC)

    ts: datetime | None = None
    rest = line
    m_ts = _PDNS_TS_RE.match(rest)
    if m_ts:
        ts = _parse_pdns_ts(
            m_ts.group("mon"),
            m_ts.group("day"),
            m_ts.group("h"),
            m_ts.group("m"),
            m_ts.group("s"),
            now=now,
        )
        rest = rest[m_ts.end() :]

    if ts is None:
        ts = now

    m_q = _PDNS_QUERY_RE.search(rest)
    if m_q is None:
        # Non-query log line (banner, status, error). Surface the raw
        # text without parsed fields so the storage filter can drop it.
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

    client_ip = m_q.group("v6") or m_q.group("v4")
    try:
        client_port: int | None = int(m_q.group("port"))
    except (TypeError, ValueError):
        client_port = None

    qname = (m_q.group("qname") or "").strip() or None
    qtype = (m_q.group("qtype") or "").strip().upper() or None

    # Drop CHAOS-class probes — see ``_CHAOS_PROBE_QNAMES`` above.
    if qname and qname.rstrip(".").lower() in _CHAOS_PROBE_QNAMES:
        return None

    return ParsedQueryLine(
        ts=ts,
        client_ip=client_ip,
        client_port=client_port,
        qname=qname,
        # PowerDNS doesn't emit qclass in its query log shape; default
        # to "IN" since 99.9% of queries are class IN and the operator
        # filter UI defaults there too.
        qclass="IN" if qname else None,
        qtype=qtype,
        flags=None,
        view=None,
        raw=line,
    )


__all__ = ["parse_query_line"]
