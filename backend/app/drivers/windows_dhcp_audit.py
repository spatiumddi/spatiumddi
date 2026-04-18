"""Windows DHCP audit log reader — parses ``DhcpSrvLog-<Day>.log``.

Complements ``windows_events.py`` (Windows Event Log reads) with the
per-lease audit trail that Windows DHCP writes to CSV-style files in
``C:\\Windows\\System32\\dhcp\\``. One file per weekday, rotating; on
by default on every Windows DHCP role. The event log's Operational
log covers service-level events (start/stop, bindings); this file
covers lease-level events (grants, renewals, releases, NACKs, DNS
update results, conflict detections).

Each line in the file is CSV: ``ID, Date, Time, Description, IP, Host,
MAC, User, TransactionID, QResult, Probationtime, CorrelationID,
Dhcid``. The file starts with a ~20-line header block explaining the
event-code mapping — we skip past that to the ``ID,Date,Time,…``
column row and feed the remainder to ``ConvertFrom-Csv``.

Event codes we recognise (mapped to human labels below). Codes not in
the map come through with ``event_label = f"Code {code}"`` so new
Windows releases that add codes don't get dropped.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date as _date
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Event-code → human label. Sourced from Microsoft's DHCP Server audit
# log documentation. Not exhaustive — anything unknown surfaces as
# "Code <n>" so new codes don't vanish.
_EVENT_LABELS: dict[int, str] = {
    0: "Log started",
    1: "Log stopped",
    2: "Log paused (low disk)",
    10: "Lease granted",
    11: "Lease renewed",
    12: "Lease released",
    13: "IP conflict detected (ping)",
    14: "Scope exhausted",
    15: "Lease denied",
    16: "Lease deleted",
    17: "Lease expired",
    18: "NACK sent",
    20: "BOOTP address leased",
    21: "BOOTP pool exhausted",
    22: "BOOTP request",
    23: "BOOTP lease expired",
    24: "Scavenged",
    25: "0-address scope full",
    30: "DNS update requested",
    31: "DNS update failed",
    32: "DNS update succeeded",
    33: "Packet dropped (MAC filter)",
    34: "Packet dropped (NAP)",
    35: "IPv6 lease revoked",
    50: "Unreachable domain",
    51: "Authorization succeeded",
    52: "Upgrade failed",
    53: "Upgrade succeeded",
    54: "Authorization failed",
    55: "Authorized (servicing)",
    56: "Authorization failed (no DS)",
    57: "Server found authoritative",
    58: "Could not find domain",
    59: "Network failure",
    60: "No DC responds",
    61: "Server found in forest",
    62: "Standby — not authoritative",
    63: "Restarting authorization",
    64: "Authorization cycle complete",
}


_WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def current_weekday_code() -> str:
    """Return the three-letter code (``Mon`` / ``Tue`` / …) for today."""
    # ``datetime.weekday()`` returns Mon=0..Sun=6; our tuple is Sun=0..Sat=6.
    wd = _date.today().weekday()  # Mon=0..Sun=6
    py_to_ours = (1, 2, 3, 4, 5, 6, 0)
    return _WEEKDAYS[py_to_ours[wd]]


def _build_script(*, day: str, max_events: int) -> str:
    """Render the PowerShell that reads + parses the log for ``day``.

    The file is read with ``Get-Content -Raw``; we find the column
    header line with ``-match`` and feed everything from there to
    ``ConvertFrom-Csv``. Missing file (DHCP role disabled / weekday's
    file doesn't exist yet) → ``[]``, not an error.
    """
    # Hard cap at 2000 — the files rotate daily, so realistic sizes
    # are a few hundred to tens of thousands of lines. Paging later if
    # we need it.
    capped = max(1, min(int(max_events), 2000))
    # DHCP audit log sits under the SystemRoot regardless of version;
    # SystemRoot is ``C:\Windows`` on standard installs. Using the env
    # var makes the script portable across odd system drives.
    return f"""
$ErrorActionPreference = 'Stop'
$path = Join-Path $env:SystemRoot "System32\\dhcp\\DhcpSrvLog-{day}.log"
if (-not (Test-Path $path)) {{
    Write-Output '[]'
    exit 0
}}
try {{
    $raw = Get-Content -Path $path -Raw -Encoding Unicode -ErrorAction Stop
    # Some DHCP builds write the log in ASCII instead of UTF-16; detect
    # by checking for a BOM-less ASCII ID line early on. Fall back to
    # default encoding if the Unicode read looks suspicious.
    if (-not ($raw -match 'ID,\\s?Date,\\s?Time,')) {{
        $raw = Get-Content -Path $path -Raw -ErrorAction Stop
    }}
}} catch {{
    # Access denied / locked by DHCP service rotation — treat as empty
    # instead of 500'ing the whole page. Caller can surface a warning.
    Write-Output '[]'
    exit 0
}}
$lines = $raw -split "`r?`n"
$headerIdx = -1
for ($i = 0; $i -lt $lines.Length; $i++) {{
    if ($lines[$i] -match '^ID,\\s?Date,\\s?Time,') {{
        $headerIdx = $i
        break
    }}
}}
if ($headerIdx -lt 0) {{
    Write-Output '[]'
    exit 0
}}
$csvText = ($lines[$headerIdx..($lines.Length - 1)] | Where-Object {{ $_ -and $_.Trim() }}) -join "`n"
$rows = $csvText | ConvertFrom-Csv
if (-not $rows) {{
    Write-Output '[]'
    exit 0
}}
# Take the last N (most recent — log is append-only).
$rows = @($rows)
if ($rows.Count -gt {capped}) {{
    $rows = $rows[-{capped}..-1]
}}
$rows | ConvertTo-Json -Compress -Depth 3
"""


def _parse_row(row: dict[str, Any], *, year: int) -> dict[str, Any] | None:
    """Shape one ``ConvertFrom-Csv`` row into our neutral dict.

    Returns ``None`` for unparseable rows so callers can count /
    ignore them. ``year`` is passed separately because the audit log
    only records ``MM/DD/YY`` with a two-digit year — we expand on
    parse using the current year as the pivot (files rotate weekly,
    so "previous year" is basically never an issue).
    """
    raw_id = (row.get("ID") or "").strip()
    raw_date = (row.get("Date") or "").strip()
    raw_time = (row.get("Time") or "").strip()
    if not raw_id or not raw_date or not raw_time:
        return None
    try:
        code = int(raw_id)
    except ValueError:
        return None

    # Date format in the file is ``MM/DD/YY``; we reassemble with the
    # pivot year. Two-digit year: ``YY`` < 70 → 2000+YY, else 1900+YY
    # (purely defensive; real DHCP logs from 2026 will say "26").
    iso = ""
    try:
        parts = raw_date.split("/")
        if len(parts) == 3:
            mm, dd, yy = parts
            yy_int = int(yy)
            full_year = 2000 + yy_int if yy_int < 70 else 1900 + yy_int
            dt = datetime.strptime(
                f"{full_year:04d}-{int(mm):02d}-{int(dd):02d} {raw_time}",
                "%Y-%m-%d %H:%M:%S",
            )
            iso = dt.isoformat()
    except ValueError:
        iso = f"{raw_date} {raw_time}"  # fallback — show the raw string

    return {
        "time": iso,
        "event_code": code,
        "event_label": _EVENT_LABELS.get(code, f"Code {code}"),
        "description": (row.get("Description") or "").strip(),
        "ip_address": (row.get("IP Address") or "").strip(),
        "hostname": (row.get("Host Name") or "").strip(),
        "mac_address": (row.get("MAC Address") or "").strip(),
        "user_name": (row.get("User Name") or "").strip(),
        "transaction_id": (row.get("TransactionID") or "").strip(),
        "q_result": (row.get("QResult") or "").strip(),
    }


async def fetch_dhcp_audit_events(
    server: Any,
    creds: dict[str, Any],
    *,
    run_ps: Callable[[Any, dict[str, Any], str], str],
    day: str | None = None,
    max_events: int = 500,
) -> list[dict[str, Any]]:
    """Read + parse the Windows DHCP audit log for ``day``.

    ``day`` is the three-letter weekday code (``Mon`` / ``Tue`` / …).
    ``None`` resolves to today. The returned list is ordered oldest →
    newest (as in the file); the UI flips that for display.
    """
    day = day or current_weekday_code()
    if day not in _WEEKDAYS:
        raise ValueError(f"day must be one of {_WEEKDAYS!r}, got {day!r}")
    script = _build_script(day=day, max_events=max_events)
    raw = await asyncio.to_thread(run_ps, server, creds, script)
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "windows_dhcp_audit_parse_failed",
            server=str(getattr(server, "id", "?")),
            day=day,
            raw=text[:400],
            error=str(exc),
        )
        return []
    items: list[dict[str, Any]] = parsed if isinstance(parsed, list) else [parsed]
    year = _date.today().year
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = _parse_row(it, year=year)
        if row is not None:
            out.append(row)
    return out


__all__ = [
    "current_weekday_code",
    "fetch_dhcp_audit_events",
]
