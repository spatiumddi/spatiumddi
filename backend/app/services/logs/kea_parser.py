"""Kea ``kea-dhcp4`` log line parser.

Stock Kea log line shape (default ``output_options`` formatter)::

    2026-04-25 16:30:01.123 INFO  [kea-dhcp4.leases/12345.139...] DHCP4_LEASE_ALLOC [hwtype=1 aa:bb:cc:dd:ee:ff], cid=[no info], tid=0x12345678: lease 192.0.2.10 has been allocated for 3600 seconds

Fields we extract:

* timestamp (UTC, since the agent runs the container in UTC)
* severity (``INFO`` / ``DEBUG`` / ``WARN`` / ``ERROR`` / ``FATAL``)
* code (the all-caps Kea log message id, e.g. ``DHCP4_LEASE_ALLOC``)
* client MAC (after ``hwtype=N`` if present)
* lease IP (the first dotted-quad after ``lease ``)
* transaction id (after ``tid=0x``)

Anything we can't extract becomes ``None``; the raw line is always
preserved so the UI can still show messages we don't fully
recognise (Kea has hundreds of log codes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

# 2026-04-25 16:30:01.123 INFO  [kea-dhcp4...] DHCP4_LEASE_ALLOC ...
_KEA_LINE_RE: Final = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+"
    r"(?P<severity>[A-Z]+)\s+"
    r"\[(?P<logger>[^\]]+)\]\s+"
    r"(?P<code>[A-Z][A-Z0-9_]+)"
    r"(?P<rest>.*)$"
)

_MAC_RE: Final = re.compile(r"(?:hwtype=\d+\s+)?(?P<mac>(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})")

# ``lease 192.0.2.10`` or ``address 192.0.2.10`` (Kea uses both
# depending on the log code). Matches IPv4 only; v6 is its own daemon.
_LEASE_IP_RE: Final = re.compile(
    r"\b(?:lease|address|client-address|allocated|assigning)\s+"
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\b"
)

_TID_RE: Final = re.compile(r"tid=(?:0x)?(?P<tid>[0-9a-fA-F]+)")


@dataclass(frozen=True)
class ParsedDHCPLine:
    ts: datetime
    severity: str | None
    code: str | None
    mac_address: str | None
    ip_address: str | None
    transaction_id: str | None
    raw: str


def _parse_kea_ts(value: str) -> datetime | None:
    try:
        if "." in value:
            main, frac = value.split(".", 1)
            micro = int(frac.ljust(6, "0")[:6])
        else:
            main, micro = value, 0
        date_part, time_part = main.split(maxsplit=1)
        y, mon, d = (int(x) for x in date_part.split("-"))
        h, m, s = (int(x) for x in time_part.split(":"))
        return datetime(y, mon, d, h, m, s, micro, tzinfo=UTC)
    except (ValueError, IndexError):
        return None


_MAX_LINE_LEN: Final = 4096


def parse_kea_line(line: str, *, fallback_ts: datetime | None = None) -> ParsedDHCPLine | None:
    """Parse one Kea DHCPv4 log line.

    Returns ``None`` for empty lines. Lines the regex doesn't match
    still produce a ``ParsedDHCPLine`` with the raw text preserved.

    Lines are truncated to ``_MAX_LINE_LEN`` before matching to bound
    the cost of regex execution against agent-supplied input. The
    Kea regex is anchored + bounded so it doesn't have an obvious
    polynomial path, but the cap is cheap defence-in-depth and
    matches the bind9_parser pattern.
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    if len(line) > _MAX_LINE_LEN:
        line = line[:_MAX_LINE_LEN]

    m = _KEA_LINE_RE.match(line)
    if not m:
        return ParsedDHCPLine(
            ts=fallback_ts or datetime.now(UTC),
            severity=None,
            code=None,
            mac_address=None,
            ip_address=None,
            transaction_id=None,
            raw=line,
        )

    ts = _parse_kea_ts(m.group("ts")) or fallback_ts or datetime.now(UTC)
    rest = m.group("rest") or ""

    mac_m = _MAC_RE.search(rest)
    ip_m = _LEASE_IP_RE.search(rest)
    tid_m = _TID_RE.search(rest)

    return ParsedDHCPLine(
        ts=ts,
        severity=m.group("severity") or None,
        code=m.group("code") or None,
        mac_address=(mac_m.group("mac").lower() if mac_m else None),
        ip_address=(ip_m.group("ip") if ip_m else None),
        transaction_id=(tid_m.group("tid").lower() if tid_m else None),
        raw=line,
    )


__all__ = ["ParsedDHCPLine", "parse_kea_line"]
