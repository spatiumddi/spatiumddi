"""RDAP (Registration Data Access Protocol) client for domain lookups.

RDAP is the JSON-returning successor to WHOIS. The IANA bootstrap
registry at ``data.iana.org/rdap/dns.json`` maps each TLD to its
authoritative RDAP server URL — we fetch + cache that JSON and use
it to route per-domain queries to the right server.

(``rdap.iana.org/domain/<n>`` itself returns 404 for any real-world
domain — it's a *registry*, not a query proxy. An earlier shape that
relied on it failed silently for everything but a couple of well-known
test domains.)

Coverage of the major TLDs (com / net / org / io / dev / app / co.uk
etc) is good enough that we ship RDAP-only in v1. A legacy WHOIS
fallback for ccTLDs that don't run RDAP yet (a shrinking set) lands
in a follow-up if operator demand surfaces.

This module is intentionally side-effect free — :func:`lookup_domain`
just returns a dict (or ``None`` on failure). The caller is responsible
for writing the result back to the DB and recomputing derived fields
(``whois_state`` / ``nameserver_drift``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Per-call ceiling: 10 s for the response, 15 s total transport budget
# (covers the redirect chain to the TLD's RDAP server). Tight enough
# that a synchronous "Refresh WHOIS" click doesn't block the UI for
# minutes when a TLD's RDAP server is slow.
_PER_REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0)
_TOTAL_TIMEOUT_SECONDS = 15.0

# IANA RDAP bootstrap registry — JSON map of TLD → RDAP base URL.
# Refreshed once per ``_BOOTSTRAP_TTL_SECONDS``; on fetch failure we
# fall back to the previous in-memory copy to keep refreshes working
# during a transient IANA outage.
_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
_BOOTSTRAP_TTL_SECONDS = 6 * 3600

_bootstrap_lock = asyncio.Lock()
_bootstrap_cache: dict[str, str] | None = None
_bootstrap_fetched_at: datetime | None = None


async def _get_bootstrap() -> dict[str, str]:
    """Return a TLD → RDAP-base map. Fetches IANA's bootstrap registry
    on first call and re-fetches once per :data:`_BOOTSTRAP_TTL_SECONDS`.
    """
    global _bootstrap_cache, _bootstrap_fetched_at  # noqa: PLW0603

    now = datetime.now(UTC)
    cache = _bootstrap_cache
    fetched = _bootstrap_fetched_at
    if (
        cache is not None
        and fetched is not None
        and (now - fetched).total_seconds() < _BOOTSTRAP_TTL_SECONDS
    ):
        return cache

    async with _bootstrap_lock:
        # Re-check inside the lock to avoid a thundering-herd refetch.
        if (
            _bootstrap_cache is not None
            and _bootstrap_fetched_at is not None
            and (now - _bootstrap_fetched_at).total_seconds() < _BOOTSTRAP_TTL_SECONDS
        ):
            return _bootstrap_cache

        try:
            async with httpx.AsyncClient(timeout=_PER_REQUEST_TIMEOUT) as client:
                resp = await client.get(_BOOTSTRAP_URL)
            if resp.status_code != 200:
                raise RuntimeError(f"bootstrap http {resp.status_code}")
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — fall back to stale cache
            logger.info(
                "rdap_bootstrap_fetch_failed",
                error=str(exc),
                stale_cache=_bootstrap_cache is not None,
            )
            if _bootstrap_cache is not None:
                return _bootstrap_cache
            return {}

        # ``services`` is a list of [tlds, urls] pairs. Flatten to
        # tld → first-https-url (preferred) | first-url so a single
        # lookup is a dict access.
        new_map: dict[str, str] = {}
        for entry in payload.get("services", []):
            if not (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], list)
                and isinstance(entry[1], list)
            ):
                continue
            tlds, urls = entry
            url_list = [u for u in urls if isinstance(u, str) and u.strip()]
            if not url_list:
                continue
            chosen = next((u for u in url_list if u.startswith("https://")), url_list[0])
            chosen = chosen.rstrip("/") + "/"
            for tld in tlds:
                if isinstance(tld, str) and tld.strip():
                    new_map[tld.strip().lower()] = chosen

        _bootstrap_cache = new_map
        _bootstrap_fetched_at = now
        return new_map


def _domain_to_tld(name: str) -> str | None:
    """Extract the public-facing TLD label from a domain. ``foo.bar.com``
    → ``com``. Multi-label TLDs like ``co.uk`` aren't matched directly —
    the bootstrap key is always the rightmost label since IANA delegates
    per-TLD; ``co.uk`` itself is served by the ``uk`` RDAP base.
    """
    parts = [p for p in name.strip().rstrip(".").lower().split(".") if p]
    return parts[-1] if parts else None


def _parse_rdap_datetime(value: Any) -> datetime | None:
    """Best-effort RFC 3339 → ``datetime`` parser.

    RDAP timestamps are RFC 3339 strings, but in the wild we see two
    common variants: ``2026-04-30T12:00:00Z`` and
    ``2026-04-30T12:00:00.123456Z``. Python's ``fromisoformat`` handles
    both as long as we swap ``Z`` → ``+00:00``. Returns ``None`` on
    anything we can't parse; the caller treats that as "field missing"
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


def _extract_event_dates(events: list[dict[str, Any]] | None) -> dict[str, datetime | None]:
    """Pull the three event dates we care about out of the RDAP
    ``events`` array.

    RDAP ``eventAction`` strings we map:

    * ``registration`` → ``registered_at``
    * ``expiration`` → ``expires_at``
    * ``last changed`` / ``last update of RDAP database`` → fallback
      for ``last_renewed_at`` (the RDAP spec doesn't have a direct
      "last renewed" event; some registries use ``last changed`` for
      this, others surface it as ``transfer``).
    """
    out: dict[str, datetime | None] = {
        "registered_at": None,
        "expires_at": None,
        "last_renewed_at": None,
    }
    if not events:
        return out
    for ev in events:
        if not isinstance(ev, dict):
            continue
        action = str(ev.get("eventAction") or "").lower().strip()
        when = _parse_rdap_datetime(ev.get("eventDate"))
        if when is None:
            continue
        if action == "registration" and out["registered_at"] is None:
            out["registered_at"] = when
        elif action == "expiration" and out["expires_at"] is None:
            out["expires_at"] = when
        elif action in {"last changed", "last update of rdap database", "transfer"}:
            # Take the most recent one — multiple "last changed" rows
            # do happen on some registries.
            existing = out["last_renewed_at"]
            if existing is None or when > existing:
                out["last_renewed_at"] = when
    return out


def _extract_registrar(entities: list[dict[str, Any]] | None) -> str | None:
    """Walk the RDAP ``entities`` array looking for the registrar
    entity (role contains ``"registrar"``) and return its display
    name. Falls back to None if not present.
    """
    if not entities:
        return None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles") or []
        if not any("registrar" in str(r).lower() for r in roles):
            continue
        # vCard: ["vcard", [["fn", {}, "text", "GoDaddy.com, LLC"], ...]]
        vcard = ent.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) > 1 and isinstance(vcard[1], list):
            for prop in vcard[1]:
                if (
                    isinstance(prop, list)
                    and len(prop) >= 4
                    and prop[0] == "fn"
                    and isinstance(prop[3], str)
                ):
                    return prop[3]
        # Some registries put the name in the ``handle`` instead.
        handle = ent.get("handle")
        if isinstance(handle, str) and handle:
            return handle
    return None


def _extract_registrant_org(entities: list[dict[str, Any]] | None) -> str | None:
    """Walk the RDAP ``entities`` array looking for the registrant
    entity (role contains ``"registrant"``) and return its
    organization name. Falls back to ``fn`` if no ``org`` is present.
    """
    if not entities:
        return None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles") or []
        if not any("registrant" in str(r).lower() for r in roles):
            continue
        vcard = ent.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) > 1 and isinstance(vcard[1], list):
            org_value: str | None = None
            fn_value: str | None = None
            for prop in vcard[1]:
                if not (isinstance(prop, list) and len(prop) >= 4):
                    continue
                key = prop[0]
                val = prop[3]
                if key == "org" and isinstance(val, str):
                    org_value = val
                elif key == "fn" and isinstance(val, str):
                    fn_value = val
            return org_value or fn_value
    return None


def _extract_nameservers(payload: dict[str, Any]) -> list[str]:
    """Flatten the RDAP ``nameservers`` array into a sorted lowercase
    list of LDH names. Trailing dots stripped for stable comparison
    against the operator-supplied ``expected_nameservers`` list.
    """
    raw = payload.get("nameservers")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for ns in raw:
        if not isinstance(ns, dict):
            continue
        ldh = ns.get("ldhName")
        if isinstance(ldh, str) and ldh:
            out.append(ldh.strip().rstrip(".").lower())
    # Stable sort for deterministic equality checks downstream.
    return sorted(set(out))


def _extract_dnssec_signed(payload: dict[str, Any]) -> bool:
    """RDAP signals DNSSEC via ``secureDNS.delegationSigned`` (bool).

    Some registries omit the section entirely when the domain is
    unsigned; we treat absence as ``False``. ``dsData`` array length
    > 0 is a secondary signal we also accept.
    """
    sec = payload.get("secureDNS")
    if not isinstance(sec, dict):
        return False
    if sec.get("delegationSigned") is True:
        return True
    ds = sec.get("dsData")
    if isinstance(ds, list) and len(ds) > 0:
        return True
    return False


async def lookup_domain(name: str) -> dict[str, Any] | None:
    """Fetch RDAP data for ``name`` and return a normalised dict.

    Returns ``None`` on any failure (404, 5xx, timeout, transport
    error). The caller treats that as ``whois_state="unreachable"``.

    The returned dict shape::

        {
            "registrar": str | None,
            "registrant_org": str | None,
            "registered_at": datetime | None,
            "expires_at": datetime | None,
            "last_renewed_at": datetime | None,
            "nameservers": list[str],   # lowercase, sorted, no trailing dot
            "dnssec_signed": bool,
            "raw": dict,                # full RDAP response payload
        }

    Idempotent + side-effect free — the caller is responsible for any
    DB writes.
    """
    if not name or not name.strip():
        return None
    target = name.strip().rstrip(".").lower()

    tld = _domain_to_tld(target)
    if tld is None:
        return None

    bootstrap = await _get_bootstrap()
    base = bootstrap.get(tld)
    if base is None:
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error=f"no RDAP base for tld={tld}",
        )
        return None

    url = f"{base}domain/{target}"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_PER_REQUEST_TIMEOUT,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error=f"transport: {exc}",
        )
        return None
    except Exception as exc:  # noqa: BLE001 — RDAP failure should never 500 the API
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error=f"unexpected: {exc}",
        )
        return None

    if resp.status_code == 404:
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error="http 404",
        )
        return None
    if resp.status_code >= 500:
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error=f"http {resp.status_code}",
        )
        return None
    if resp.status_code != 200:
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error=f"http {resp.status_code}",
        )
        return None

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error=f"invalid json: {exc}",
        )
        return None
    if not isinstance(payload, dict):
        logger.info(
            "domain_rdap_unreachable",
            domain=target,
            error="payload not an object",
        )
        return None

    entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    dates = _extract_event_dates(events)

    return {
        "registrar": _extract_registrar(entities),
        "registrant_org": _extract_registrant_org(entities),
        "registered_at": dates["registered_at"],
        "expires_at": dates["expires_at"],
        "last_renewed_at": dates["last_renewed_at"],
        "nameservers": _extract_nameservers(payload),
        "dnssec_signed": _extract_dnssec_signed(payload),
        "raw": payload,
    }


# ── Derived-field helper ────────────────────────────────────────────
#
# ``derive_whois_state`` is the single source of truth for the
# ``Domain.whois_state`` bucket label. It's pure / side-effect-free
# and used by both the synchronous ``POST /domains/{id}/refresh-whois``
# endpoint and the scheduled refresh task in
# ``app.tasks.domain_whois_refresh``. Decision rules mirror issue #87:
#
#   1. RDAP returned no data → ``unreachable``.
#   2. ``expires_at`` in the past → ``expired``.
#   3. ``expires_at`` within ``_EXPIRING_DAYS`` (30) → ``expiring``.
#   4. Operator pinned ``expected_nameservers`` and the actual list
#      (lowercase + sorted) doesn't match → ``drift``.
#   5. Otherwise → ``ok``.

_EXPIRING_DAYS = 30


def derive_whois_state(
    *,
    rdap_returned_data: bool,
    expires_at: datetime | None,
    expected_nameservers: list[str],
    actual_nameservers: list[str],
    now: datetime | None = None,
) -> str:
    """Compute the ``Domain.whois_state`` bucket label.

    Pure helper. The order of checks matters — expiry beats drift so
    a domain that's both about to expire AND has NS drift surfaces the
    more urgent label. The day-window granularity for alert severity
    (soft/warning/critical) lives in the alert evaluator; this function
    only collapses to the four-bucket UI state.
    """
    if not rdap_returned_data:
        return "unreachable"
    when = now or datetime.now(UTC)
    if expires_at is not None:
        # Postgres returns timezone-aware datetimes; defensive coerce
        # in case a caller passes a naive one.
        exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if exp <= when:
            return "expired"
        if exp - when <= timedelta(days=_EXPIRING_DAYS):
            return "expiring"
    if expected_nameservers:
        # Defensive lowercase + sort in case a row predates normalisation.
        exp_set = sorted({s.strip().rstrip(".").lower() for s in expected_nameservers if s})
        act_set = sorted({s.strip().rstrip(".").lower() for s in actual_nameservers if s})
        if exp_set and exp_set != act_set:
            return "drift"
    return "ok"


def normalise_nameservers(values: list[str] | None) -> list[str]:
    """Lowercase + trailing-dot strip + de-dupe + sort.

    Pure helper used by the drift comparator on both the endpoint and
    the task path. Stable sort makes equality checks against
    ``expected_nameservers`` deterministic.
    """
    if not values:
        return []
    out: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        s = v.strip().rstrip(".").lower()
        if s:
            out.add(s)
    return sorted(out)


def compute_nameserver_drift(
    expected: list[str] | None,
    actual: list[str] | None,
) -> bool:
    """True iff the operator pinned at least one expected NS AND the
    actual list (post-normalisation) differs from it. Returns False
    when ``expected`` is empty — drift is opt-in, the operator has to
    pin an expectation before drift can fire."""
    exp = normalise_nameservers(expected)
    act = normalise_nameservers(actual)
    return bool(exp) and exp != act


# Total-timeout constant exported for the scheduled-task implementation
# — it should bound a per-row refresh end-to-end so a slow registry
# can't stall the whole sweep.
__all__ = [
    "lookup_domain",
    "derive_whois_state",
    "normalise_nameservers",
    "compute_nameserver_drift",
    "_TOTAL_TIMEOUT_SECONDS",
    "_EXPIRING_DAYS",
]
