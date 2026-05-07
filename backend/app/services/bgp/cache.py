"""Tiny in-process TTL cache for BGP enrichment fetches (issue
#122).

Mirrors the pattern in :mod:`app.services.rpki_roa` — module-level
dict keyed on ``(source, query)`` with a fetched-at timestamp. The
cache is per-worker, deliberately not Redis-backed: the upstream
data is public + small + we don't need cross-worker coherency, and
running through Redis would just add latency without buying us
anything.

Two TTLs are exported as constants:

* :data:`RIPESTAT_TTL_SECONDS` — 6 h (RIPEstat data refreshes a few
  times a day; 6 h is comfortably below the upstream cadence and
  caps the per-worker heat on the public API).
* :data:`PEERINGDB_TTL_SECONDS` — 24 h (PeeringDB asks consumers to
  cache; the registered-network metadata changes infrequently).
"""

from __future__ import annotations

import time
from typing import Any

RIPESTAT_TTL_SECONDS = 6 * 60 * 60
PEERINGDB_TTL_SECONDS = 24 * 60 * 60

# Keyed by ``(source, query_key)`` → ``(fetched_at, payload)``.
_cache: dict[tuple[str, str], tuple[float, Any]] = {}


def get(source: str, query_key: str, ttl_seconds: int) -> Any | None:
    """Return cached payload for the key, or ``None`` if missing /
    stale.
    """
    entry = _cache.get((source, query_key))
    if entry is None:
        return None
    fetched_at, payload = entry
    if time.monotonic() - fetched_at > ttl_seconds:
        _cache.pop((source, query_key), None)
        return None
    return payload


def set_(source: str, query_key: str, payload: Any) -> None:
    """Store ``payload`` for ``(source, query_key)``."""
    _cache[(source, query_key)] = (time.monotonic(), payload)


def clear() -> None:
    """Drop the whole cache. Wired up for tests + the (future)
    Settings → BGP Enrichment → Clear cache button.
    """
    _cache.clear()
