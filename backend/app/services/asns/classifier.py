"""ASN classification helpers.

Two derivations per the design in issue #85:

* ``derive_kind`` — public vs. private. Private ranges per RFC 6996
  (16-bit ``64512..65534``) and RFC 7300 (32-bit ``4_200_000_000..
  4_294_967_294``). The two reserved boundary values ``65535`` and
  ``4_294_967_295`` are reserved-for-documentation/last-AS — we treat
  them as private since they cannot legitimately appear on the public
  internet. ``0`` is also reserved (RFC 7607); included in the private
  set so a stray ``0`` row doesn't try to refresh against a RIR.

* ``derive_registry`` — which RIR delegated the AS, looked up from a
  static IANA ASN block-allocation table at
  ``backend/app/data/asn_registry_delegations.json``. The table is a
  hand-curated snapshot (Phase 1) — pulling the live IANA XML can
  land later if drift becomes a real problem. Private numbers always
  return ``unknown`` because ``registry`` doesn't apply to them.

Pure functions with no DB / IO side effects so they can be called
from migration data-fix scripts and tests without setup.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

# RFC 6996 + RFC 7300 + RFC 7607: the contiguous private / reserved
# ranges. Stored as ``(low, high)`` inclusive tuples so a simple
# ``low <= n <= high`` check decides ``kind``.
PRIVATE_AS_RANGES: tuple[tuple[int, int], ...] = (
    (0, 0),  # RFC 7607 — reserved, never assigned
    (64512, 65534),  # RFC 6996 — 16-bit private use
    (65535, 65535),  # RFC 7300 — last 16-bit AS, reserved
    (4_200_000_000, 4_294_967_294),  # RFC 6996 — 32-bit private use
    (4_294_967_295, 4_294_967_295),  # RFC 7300 — last 32-bit AS, reserved
)

REGISTRIES = frozenset({"arin", "ripe", "apnic", "lacnic", "afrinic", "unknown"})

ASNKind = Literal["public", "private"]
ASNRegistry = Literal["arin", "ripe", "apnic", "lacnic", "afrinic", "unknown"]


# ── Range bounds ─────────────────────────────────────────────────────


def _validate_number(number: int) -> None:
    """Raise ``ValueError`` if ``number`` is outside the 32-bit AS range."""
    if not isinstance(number, int):
        raise TypeError(f"AS number must be int, got {type(number).__name__}")
    if number < 0 or number > 4_294_967_295:
        raise ValueError(f"AS number {number} is outside the 32-bit range")


# ── Kind derivation ──────────────────────────────────────────────────


def derive_kind(number: int) -> ASNKind:
    """Return ``private`` if ``number`` falls inside any reserved /
    private-use range, else ``public``."""
    _validate_number(number)
    for lo, hi in PRIVATE_AS_RANGES:
        if lo <= number <= hi:
            return "private"
    return "public"


# ── Registry delegation table ────────────────────────────────────────


@lru_cache(maxsize=1)
def _load_delegation_table() -> tuple[tuple[int, int, str], ...]:
    """Read + sort the JSON delegation table once. Kept module-private
    so callers don't depend on the on-disk shape."""
    raw = files("app.data").joinpath("asn_registry_delegations.json").read_text()
    payload = json.loads(raw)
    rows: list[tuple[int, int, str]] = []
    for entry in payload.get("ranges", []):
        registry = entry["registry"]
        if registry not in REGISTRIES:
            # Defensive: a typo in the JSON shouldn't make us crash at
            # boot. ``unknown`` is always safe.
            registry = "unknown"
        rows.append((int(entry["start"]), int(entry["end"]), registry))
    rows.sort()
    return tuple(rows)


def derive_registry(number: int) -> ASNRegistry:
    """Look up the RIR delegation for a public AS number.

    Returns ``unknown`` for private ranges (registry doesn't apply)
    and for any public number that isn't covered by the delegation
    table — which is normal for newly-issued blocks the snapshot
    hasn't caught up with yet. The RDAP refresh job (follow-up issue)
    can override the stored value once it's populated holder data.
    """
    _validate_number(number)
    if derive_kind(number) == "private":
        return "unknown"

    table = _load_delegation_table()
    # Linear scan: ~120 rows, called once per write — not a hot path
    # worth optimising. ``bisect`` would help if we ever have 10_000+
    # entries.
    for lo, hi, reg in table:
        if lo <= number <= hi:
            return reg  # type: ignore[return-value]
    return "unknown"


# ── Public convenience wrapper ──────────────────────────────────────


def classify_asn(number: int) -> tuple[ASNKind, ASNRegistry]:
    """Return ``(kind, registry)`` in one call. Convenience for
    routes that need both."""
    kind = derive_kind(number)
    if kind == "private":
        return kind, "unknown"
    return kind, derive_registry(number)
