"""RPKI ROA pull — fetch the global ROA dump from a public mirror and
filter it down to the ROAs an AS is authorised to originate.

Two source backends today:

* **Cloudflare** — ``https://rpki.cloudflare.com/rpki.json``. Compact
  JSON shape: ``{"roas": [{"asn": "AS13335", "prefix": "1.1.1.0/24",
  "maxLength": 24, "ta": "apnic"}, ...]}``. No per-ROA validity
  windows; the dump itself is refreshed roughly every 20 minutes.
* **RIPE NCC RPKI Validator 3** —
  ``https://rpki-validator.ripe.net/api/objects/validated.json``.
  Same triple shape (``asn`` / ``prefix`` / ``maxLength`` / ``ta``)
  under a different envelope. RIPE's validator publishes the full
  validated set (not just RIPE-issued) so the data is comparable.

Neither mirror exposes ``valid_from`` / ``valid_to`` on individual
ROAs in a stable shape — both project the underlying RPKI artifacts'
notBefore/notAfter from the X.509 layer, but the public JSON drops
them. We accept that and return ``None`` for both fields; the caller
treats unknown windows as ``state="valid"`` (no expiry pressure)
rather than firing spurious alerts.

The full ROA dump is multi-MB (Cloudflare's 2026-vintage payload is
~120k entries, ~6 MB JSON). We cache it in-memory for 5 minutes via
:func:`_get_cached_roas` so a beat sweep that needs to refresh ROAs
for 50 ASNs makes a single HTTP call instead of 50.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Per-call ceiling — the global ROA dump is multi-MB so ``read`` has
# to be generous, but we cap the connect budget so a dead mirror
# doesn't stall the worker for minutes.
_PER_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0, read=30.0)

# 5-minute cache TTL keyed by source. The Cloudflare mirror itself
# refreshes every ~20 minutes so we're not at risk of serving stale
# data; this is purely a worker-side optimisation so a beat sweep
# refreshing ROAs for 50 ASNs makes one HTTP call, not 50.
_CACHE_TTL_SECONDS = 300

_SOURCE_URLS = {
    "cloudflare": "https://rpki.cloudflare.com/rpki.json",
    "ripe": "https://rpki-validator.ripe.net/api/objects/validated.json",
}

_VALID_TRUST_ANCHORS = {"arin", "ripe", "apnic", "lacnic", "afrinic"}

# Module-level cache: { source: (fetched_at_epoch, roas_list) }
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _normalise_asn_field(value: Any) -> int | None:
    """Cloudflare emits ``"AS13335"``; RIPE sometimes emits the raw
    int. Accept both shapes; return ``None`` on anything we can't
    parse so the ROA gets skipped rather than crashing the load.
    """
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        s = value.strip().upper()
        if s.startswith("AS"):
            s = s[2:]
        try:
            n = int(s)
        except ValueError:
            return None
        return n if n > 0 else None
    return None


def _normalise_trust_anchor(value: Any) -> str | None:
    """Both mirrors emit ``"arin"`` / ``"ripe"`` / ``"apnic"`` /
    ``"lacnic"`` / ``"afrinic"``; some payloads use the older
    ``"AfriNIC"`` casing. Lower-case + restrict to the known set,
    falling back to ``None`` (treated as ``"unknown"`` downstream).
    """
    if not isinstance(value, str):
        return None
    code = value.strip().lower()
    return code if code in _VALID_TRUST_ANCHORS else None


def _normalise_max_length(value: Any) -> int | None:
    """Coerce ``maxLength`` to int. Drop entries we can't parse —
    a malformed row shouldn't poison the whole load.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def _fetch_full_dump(source: str) -> list[dict[str, Any]] | None:
    """Fetch + parse the global ROA dump from one source.

    Returns ``None`` on transport failure / non-200 / malformed JSON
    so the caller can return an empty list and the per-row task
    just leaves the ROAs alone for this tick.
    """
    url = _SOURCE_URLS.get(source)
    if url is None:
        logger.warning("rpki_roa_unknown_source", source=source)
        return None

    try:
        async with httpx.AsyncClient(timeout=_PER_REQUEST_TIMEOUT) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("rpki_roa_fetch_failed", source=source, error=f"transport: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — never block the worker on a bad mirror
        logger.info("rpki_roa_fetch_failed", source=source, error=f"unexpected: {exc}")
        return None

    if resp.status_code != 200:
        logger.info("rpki_roa_fetch_failed", source=source, error=f"http {resp.status_code}")
        return None

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.info("rpki_roa_fetch_failed", source=source, error=f"invalid json: {exc}")
        return None

    if not isinstance(payload, dict):
        logger.info("rpki_roa_fetch_failed", source=source, error="payload not an object")
        return None

    # Cloudflare wraps under ``roas``; RIPE under ``roas`` too in
    # the v3 validator. ``data.roas`` is also seen on some mirrors.
    raw_roas = payload.get("roas")
    if raw_roas is None:
        nested = payload.get("data")
        if isinstance(nested, dict):
            raw_roas = nested.get("roas")
    if not isinstance(raw_roas, list):
        logger.info("rpki_roa_fetch_failed", source=source, error="no roas array")
        return None

    return raw_roas


async def _get_cached_roas(source: str) -> list[dict[str, Any]]:
    """Return the cached ROA list for ``source``, refreshing if
    older than ``_CACHE_TTL_SECONDS``. Empty list on fetch failure.
    """
    now = time.monotonic()
    cached = _cache.get(source)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    fresh = await _fetch_full_dump(source)
    if fresh is None:
        # Keep the stale cache around if we have one — better stale
        # than empty when the mirror flakes for a tick.
        if cached is not None:
            return cached[1]
        return []

    _cache[source] = (now, fresh)
    return fresh


async def fetch_roas_for_asn(asn_number: int, source: str) -> list[dict[str, Any]]:
    """Filter the cached global dump down to ROAs originated by
    ``asn_number``.

    Returned shape (one dict per ROA)::

        {
            "prefix": "1.1.1.0/24",
            "max_length": 24,
            "valid_from": None,         # public mirrors don't expose these
            "valid_to": None,           # — caller falls back to state="valid"
            "trust_anchor": "apnic",    # may be None on malformed rows
        }

    # TODO: pull validity windows from a Routinator instance when one
    # is configured — Routinator's API exposes per-VRP notBefore/notAfter.
    # The two public mirrors used here don't surface them.

    Returns an empty list on fetch failure so the caller's reconcile
    pass treats the AS as "no ROAs this tick" rather than wiping the
    existing rows. The next successful tick re-syncs.
    """
    roas = await _get_cached_roas(source)
    if not roas:
        return []

    out: list[dict[str, Any]] = []
    for raw in roas:
        if not isinstance(raw, dict):
            continue
        # Both mirrors use ``asn``; RIPE older payloads use ``customerASN``.
        asn_field = raw.get("asn") if "asn" in raw else raw.get("customerASN")
        n = _normalise_asn_field(asn_field)
        if n is None or n != asn_number:
            continue

        prefix = raw.get("prefix")
        if not isinstance(prefix, str) or not prefix.strip():
            continue

        max_length = _normalise_max_length(raw.get("maxLength") or raw.get("max_length"))
        if max_length is None:
            continue

        # ``ta`` is Cloudflare's key; ``trustAnchor`` / ``ta_name``
        # show up in some RIPE variants.
        ta_raw = raw.get("ta") or raw.get("trustAnchor") or raw.get("ta_name")
        ta = _normalise_trust_anchor(ta_raw)

        out.append(
            {
                "prefix": prefix.strip(),
                "max_length": int(max_length),
                "valid_from": None,
                "valid_to": None,
                "trust_anchor": ta,
            }
        )
    return out


def _clear_cache_for_test() -> None:
    """Reset the module-level cache. Test-only; never call in prod."""
    _cache.clear()


__all__ = ["fetch_roas_for_asn", "_clear_cache_for_test"]
