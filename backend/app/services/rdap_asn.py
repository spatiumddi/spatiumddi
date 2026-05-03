"""RDAP (Registration Data Access Protocol) client for autnum lookups.

ASN-side counterpart to :mod:`app.services.rdap` (which handles domain
RDAP). IANA's ``rdap.iana.org/autnum/<n>`` is documented as a
redirect-bootstrap but in practice returns ``501 Not Implemented`` for
direct queries — it's only a bootstrap *registry*, not a query
service. We instead derive the RIR from the bundled IANA delegation
snapshot (see :mod:`app.services.asns.classifier`) and query that
RIR's RDAP base directly.

Phase 2 of issue #85 ships RDAP-only — legacy ``whois.<rir>.net`` text
parsing is a follow-up if real-world coverage gaps surface. Today's
RDAP coverage of the RIRs is essentially complete.

Side-effect free: :func:`lookup_asn` returns a normalised dict (or
``None`` on failure). The caller writes the result back to ``asn`` and
recomputes ``whois_state`` based on the diff against the previous
snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog

from app.services.asns.classifier import derive_registry

logger = structlog.get_logger(__name__)

# Match domain RDAP timing budget so the manual "Refresh now" click
# doesn't block any longer than the equivalent domain refresh.
_PER_REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0)
_TOTAL_TIMEOUT_SECONDS = 15.0

# Per-RIR RDAP base. Lifted from the IANA RDAP bootstrap registry
# (https://data.iana.org/rdap/asn.json). Hardcoded so the refresh task
# doesn't take a network dependency on the bootstrap fetch — the RIR
# RDAP endpoints rarely move and a bad URL here just falls back to
# ``unreachable`` like any other RDAP failure.
_RIR_RDAP_BASE: dict[str, str] = {
    "arin": "https://rdap.arin.net/registry/autnum",
    "ripe": "https://rdap.db.ripe.net/autnum",
    "apnic": "https://rdap.apnic.net/autnum",
    "lacnic": "https://rdap.lacnic.net/rdap/autnum",
    "afrinic": "https://rdap.afrinic.net/rdap/autnum",
}

# RDAP ``port43`` field hints at the underlying registry; we map the
# common values to our internal RIR codes for the ``registry`` field
# fallback (the IANA delegation snapshot remains the primary source).
_PORT43_TO_REGISTRY = {
    "whois.arin.net": "arin",
    "whois.ripe.net": "ripe",
    "whois.apnic.net": "apnic",
    "whois.lacnic.net": "lacnic",
    "whois.afrinic.net": "afrinic",
}


def _parse_rdap_datetime(value: Any) -> datetime | None:
    """Best-effort RFC 3339 → ``datetime`` parser. Returns ``None`` on
    anything we can't parse — the caller treats that as "field missing"
    rather than failing the whole refresh.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _extract_holder_org(payload: dict[str, Any]) -> str | None:
    """Walk RDAP ``entities`` looking for the registrant / holder.

    For autnum responses the AS holder is usually the first entity
    with role ``"registrant"`` (some RIRs use ``"administrative"`` or
    ``"technical"`` as fallback). We pick the first entity whose
    vCard ``org`` (or ``fn`` as fallback) is non-empty, preferring
    explicit registrant rows.
    """
    entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    if not entities:
        # Some RIRs put the holder on the top-level ``name`` field directly.
        name = payload.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

    # Two-pass: explicit registrants first, then any entity with a name.
    def _from_vcard(ent: dict[str, Any]) -> str | None:
        vcard = ent.get("vcardArray")
        if not (isinstance(vcard, list) and len(vcard) > 1 and isinstance(vcard[1], list)):
            return None
        org_value: str | None = None
        fn_value: str | None = None
        for prop in vcard[1]:
            if not (isinstance(prop, list) and len(prop) >= 4):
                continue
            key = prop[0]
            val = prop[3]
            if key == "org" and isinstance(val, str) and val.strip():
                org_value = val.strip()
            elif key == "fn" and isinstance(val, str) and val.strip():
                fn_value = val.strip()
        return org_value or fn_value

    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles") or []
        if any("registrant" in str(r).lower() for r in roles):
            picked = _from_vcard(ent)
            if picked:
                return picked

    for ent in entities:
        if not isinstance(ent, dict):
            continue
        picked = _from_vcard(ent)
        if picked:
            return picked

    return None


def _extract_last_modified(payload: dict[str, Any]) -> datetime | None:
    """RDAP ``events`` array carries ``last changed`` for the last
    holder/contact update. Falls back to ``last update of RDAP
    database`` when ``last changed`` is absent.
    """
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    if not events:
        return None
    last_changed: datetime | None = None
    last_db: datetime | None = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        action = str(ev.get("eventAction") or "").lower().strip()
        when = _parse_rdap_datetime(ev.get("eventDate"))
        if when is None:
            continue
        if action == "last changed":
            if last_changed is None or when > last_changed:
                last_changed = when
        elif action == "last update of rdap database":
            if last_db is None or when > last_db:
                last_db = when
    return last_changed or last_db


def _extract_registry(payload: dict[str, Any]) -> str | None:
    """Extract the RIR code from the RDAP response.

    RDAP doesn't have a single canonical "which RIR" field; we try
    ``port43`` (whois server hostname) first, then fall back to the
    ``handle`` prefix (e.g. ``AS15169-Z`` → no clue, ``AS15169``
    → no clue either, but ARIN handles tend to look like ``AS15169``
    and RIPE like ``AS15169-MNT``). Only ``port43`` is reliable enough
    to use; everything else is best-effort and returns ``None`` if it
    can't decide.
    """
    port43 = payload.get("port43")
    if isinstance(port43, str):
        return _PORT43_TO_REGISTRY.get(port43.strip().lower())
    return None


async def lookup_asn(number: int) -> dict[str, Any] | None:
    """Fetch RDAP data for an AS number and return a normalised dict.

    Returns ``None`` on any failure (404, 5xx, timeout, transport
    error, malformed JSON). The caller treats that as
    ``whois_state="unreachable"``.

    The returned dict shape::

        {
            "holder_org": str | None,
            "registry": str | None,    # rir code from port43, may be None
            "name": str | None,        # RDAP top-level "name" (handle/asn name)
            "last_modified_at": datetime | None,
            "raw": dict,               # full RDAP response payload
        }

    Idempotent + side-effect free.
    """
    if number <= 0:
        return None

    rir = derive_registry(number)
    base = _RIR_RDAP_BASE.get(rir)
    if base is None:
        # ``derive_registry`` returns ``unknown`` for private ranges + for
        # public numbers the IANA snapshot doesn't cover. Either way we
        # have no idea where to query, so log + bail.
        logger.info(
            "asn_rdap_unreachable",
            asn=number,
            error=f"no RDAP base for registry={rir}",
        )
        return None

    url = f"{base}/{number}"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_PER_REQUEST_TIMEOUT,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("asn_rdap_unreachable", asn=number, error=f"transport: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — RDAP failure must never 500 the API
        logger.info("asn_rdap_unreachable", asn=number, error=f"unexpected: {exc}")
        return None

    if resp.status_code == 404:
        logger.info("asn_rdap_unreachable", asn=number, error="http 404")
        return None
    if resp.status_code >= 500:
        logger.info("asn_rdap_unreachable", asn=number, error=f"http {resp.status_code}")
        return None
    if resp.status_code != 200:
        logger.info("asn_rdap_unreachable", asn=number, error=f"http {resp.status_code}")
        return None

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.info("asn_rdap_unreachable", asn=number, error=f"invalid json: {exc}")
        return None
    if not isinstance(payload, dict):
        logger.info("asn_rdap_unreachable", asn=number, error="payload not an object")
        return None

    holder_org = _extract_holder_org(payload)
    registry = _extract_registry(payload)
    last_modified_at = _extract_last_modified(payload)
    name = payload.get("name") if isinstance(payload.get("name"), str) else None

    return {
        "holder_org": holder_org,
        "registry": registry,
        "name": name,
        "last_modified_at": last_modified_at,
        "raw": payload,
    }


__all__ = ["lookup_asn", "_TOTAL_TIMEOUT_SECONDS"]
