"""RIPEstat Data API client (issue #122).

RIPEstat is the workhorse routing-table data source: announced
prefixes per AS, prefix-origin lookup, routing history. Free, no
API key required, JSON envelope.

Endpoints we touch:

* ``announced-prefixes`` — what prefixes is ``AS<n>`` currently
  announcing.
* ``prefix-overview`` — for a given IP or CIDR, who's announcing
  it (origin AS), plus delegation + ROA snippet.
* ``routing-history`` — timeline of origin-AS changes for a
  prefix (catches re-homings + hijack events).
* ``as-overview`` — AS holder + announced count + RIR.

All endpoints are wrapped through
:mod:`app.services.bgp.cache` with a 6 h TTL. Failures are
surfaced as a soft ``{"available": False, "error": "..."}``
shape rather than HTTP 500 — operators behind tight egress get a
useful message instead of a crashed page.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.services.bgp._errors import classify_http_error
from app.services.bgp.cache import RIPESTAT_TTL_SECONDS
from app.services.bgp.cache import get as cache_get
from app.services.bgp.cache import set_ as cache_set

logger = structlog.get_logger(__name__)

_BASE_URL = "https://stat.ripe.net/data"
_USER_AGENT = "SpatiumDDI/0.1 (+https://github.com/spatiumddi/spatiumddi; bgp-enrichment)"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0)


async def _fetch(endpoint: str, resource: str) -> dict[str, Any]:
    """Hit ``stat.ripe.net/data/<endpoint>/data.json?resource=<resource>``.

    Cached. On any HTTP / network failure returns ``{"available":
    False, "error": "<one-line>"}`` instead of raising — the higher
    layers (tool / endpoint) know how to render that.
    """
    cache_key = f"{endpoint}|{resource}"
    cached = cache_get("ripestat", cache_key, RIPESTAT_TTL_SECONDS)
    if cached is not None:
        return cached

    url = f"{_BASE_URL}/{endpoint}/data.json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url,
                params={"resource": resource},
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        # Full str(exc) lands in the structured log; the response
        # carries only a sanitized category so we don't leak the
        # upstream URL / hostname to the client (CodeQL
        # py/stack-trace-exposure).
        logger.info(
            "ripestat_fetch_failed",
            endpoint=endpoint,
            resource=resource,
            error=str(exc),
        )
        return {"available": False, "error": classify_http_error(exc)}
    except ValueError:
        logger.info("ripestat_malformed_response", endpoint=endpoint, resource=resource)
        return {"available": False, "error": "malformed_response"}

    if not isinstance(payload, dict):
        return {"available": False, "error": "unexpected response shape"}
    payload.setdefault("data", {})
    payload["available"] = True
    cache_set("ripestat", cache_key, payload)
    return payload


async def fetch_announced_prefixes(asn: int) -> dict[str, Any]:
    """Prefixes currently announced by ``AS<asn>``.

    Returns a normalised shape::

        {
          "available": True,
          "asn": 15169,
          "prefixes": [
            {"prefix": "8.8.8.0/24", "first_seen": "2008-...",
             "last_seen": "2026-..."},
            ...
          ],
          "ipv4_count": 412,
          "ipv6_count": 18,
        }
    """
    raw = await _fetch("announced-prefixes", f"AS{asn}")
    if not raw.get("available"):
        return {"available": False, "asn": asn, "error": raw.get("error", "unavailable")}
    data = raw.get("data") or {}
    rows = data.get("prefixes") or []
    out_rows: list[dict[str, Any]] = []
    v4 = 0
    v6 = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        prefix = row.get("prefix")
        if not prefix or not isinstance(prefix, str):
            continue
        timelines = row.get("timelines") or []
        first_seen: str | None = None
        last_seen: str | None = None
        if timelines and isinstance(timelines[0], dict):
            first_seen = timelines[0].get("starttime")
            last_seen = timelines[-1].get("endtime")
        if ":" in prefix:
            v6 += 1
        else:
            v4 += 1
        out_rows.append(
            {
                "prefix": prefix,
                "first_seen": first_seen,
                "last_seen": last_seen,
            }
        )
    return {
        "available": True,
        "asn": asn,
        "prefixes": out_rows,
        "ipv4_count": v4,
        "ipv6_count": v6,
    }


async def fetch_prefix_overview(resource: str) -> dict[str, Any]:
    """Origin AS + metadata for an IP or CIDR.

    Normalised shape::

        {
          "available": True,
          "resource": "8.8.8.8",
          "prefix": "8.8.8.0/24",
          "is_less_specific": False,
          "asns": [{"asn": 15169, "holder": "GOOGLE"}],
          "block": {"resource": "8.0.0.0/9", "desc": "..."},
        }
    """
    raw = await _fetch("prefix-overview", resource)
    if not raw.get("available"):
        return {
            "available": False,
            "resource": resource,
            "error": raw.get("error", "unavailable"),
        }
    data = raw.get("data") or {}
    return {
        "available": True,
        "resource": resource,
        "prefix": data.get("resource"),
        "is_less_specific": bool(data.get("is_less_specific")),
        "asns": [
            {"asn": a.get("asn"), "holder": a.get("holder")}
            for a in (data.get("asns") or [])
            if isinstance(a, dict) and a.get("asn") is not None
        ],
        "block": data.get("block") or None,
        "announced": bool(data.get("announced")),
    }


async def fetch_routing_history(resource: str) -> dict[str, Any]:
    """Timeline of origin-AS changes for ``resource``.

    Normalised shape::

        {
          "available": True,
          "resource": "1.1.1.0/24",
          "events": [
            {"asn": 13335, "starttime": "...", "endtime": "..."},
            ...
          ],
        }
    """
    raw = await _fetch("routing-history", resource)
    if not raw.get("available"):
        return {
            "available": False,
            "resource": resource,
            "error": raw.get("error", "unavailable"),
        }
    data = raw.get("data") or {}
    by_asn = data.get("by_origin") or []
    events: list[dict[str, Any]] = []
    for entry in by_asn:
        if not isinstance(entry, dict):
            continue
        asn = entry.get("origin")
        if asn is None:
            continue
        for tl in entry.get("timelines") or []:
            events.append(
                {
                    "asn": asn,
                    "starttime": tl.get("starttime"),
                    "endtime": tl.get("endtime"),
                }
            )
    events.sort(key=lambda e: e.get("starttime") or "")
    return {"available": True, "resource": resource, "events": events}


async def fetch_as_overview(asn: int) -> dict[str, Any]:
    """RIPEstat ``as-overview`` — holder + RIR + announced summary."""
    raw = await _fetch("as-overview", f"AS{asn}")
    if not raw.get("available"):
        return {"available": False, "asn": asn, "error": raw.get("error", "unavailable")}
    data = raw.get("data") or {}
    return {
        "available": True,
        "asn": asn,
        "holder": data.get("holder"),
        "type": data.get("type"),
        "announced": bool(data.get("announced")),
        "block": data.get("block") or None,
    }
