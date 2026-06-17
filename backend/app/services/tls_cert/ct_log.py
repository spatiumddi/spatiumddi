"""Certificate Transparency log cross-reference (issue #118 Phase 3).

Queries the public crt.sh CT aggregator for every logged certificate
issued for a host/domain, so an operator can spot issuance they didn't
deploy (mis-issuance / rogue certs) next to what the probe actually sees.

OFF-PREM: this is the only part of the TLS feature that makes an outbound
call to a third party, and it leaks the queried hostname to crt.sh. It is
therefore an EXPLICIT, on-demand action only — never run by the scheduled
probe — and the matching MCP tool ships default-disabled. Best-effort:
crt.sh is frequently slow/rate-limited, so failures degrade to an empty
result with an ``error`` string rather than raising.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_CRT_SH_URL = "https://crt.sh/"
_TIMEOUT = 12.0
_MAX_ENTRIES = 100


async def lookup_ct(host: str, *, limit: int = 50) -> dict[str, Any]:
    """Return recent CT-logged certificates for ``host`` (via crt.sh).

    ``{"host", "entries": [...], "count", "error"}`` — ``error`` is set + a
    fast empty result returned on any transport / parse failure."""
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return {"host": host, "entries": [], "count": 0, "error": "no host"}
    limit = max(1, min(_MAX_ENTRIES, limit))
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                _CRT_SH_URL,
                params={"q": host, "output": "json", "exclude": "expired"},
                headers={"User-Agent": "SpatiumDDI-TLS-monitor"},
            )
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Log the detail server-side; return a generic message to the client
        # (don't echo the raw exception — info exposure).
        logger.info("ct_log_lookup_failed", host=host, error=str(exc))
        return {
            "host": host,
            "entries": [],
            "count": 0,
            "error": "CT-log lookup failed (crt.sh unreachable or rate-limited)",
        }

    if not isinstance(raw, list):
        return {"host": host, "entries": [], "count": 0, "error": "unexpected response"}

    # Dedupe on (issuer, serial) — crt.sh returns one row per log entry, so
    # the same cert appears many times.
    seen: set[tuple[str, str]] = set()
    entries: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("issuer_name", "")), str(row.get("serial_number", "")))
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "id": row.get("id"),
                "common_name": row.get("common_name"),
                "name_value": row.get("name_value"),
                "issuer_name": row.get("issuer_name"),
                "serial_number": row.get("serial_number"),
                "not_before": row.get("not_before"),
                "not_after": row.get("not_after"),
                "entry_timestamp": row.get("entry_timestamp"),
            }
        )
        if len(entries) >= limit:
            break

    # Newest issuance first.
    entries.sort(key=lambda e: str(e.get("not_before") or ""), reverse=True)
    return {"host": host, "entries": entries, "count": len(entries), "error": None}
