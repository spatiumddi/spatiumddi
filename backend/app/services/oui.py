"""OUI vendor lookup — map MAC → vendor name via the ``oui_vendor`` table.

All public functions short-circuit to empty results when
``PlatformSettings.oui_lookup_enabled`` is False, so callers can always
call them unconditionally and let the feature flag gate the work. Loaders
(bulk list / DHCP lease list) use :func:`bulk_lookup_vendors` to avoid
N+1 queries across a table page.

The ``oui_vendor`` table is replaced atomically by the
``app.tasks.oui_update`` task, so any lookup here sees a consistent
snapshot.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oui import OUIVendor
from app.models.settings import PlatformSettings

_SINGLETON_ID = 1
_MAC_DELIMS = re.compile(r"[:\-.\s]")


def _prefix_from_mac(raw: str | None) -> str | None:
    """Return the first 6 hex chars (lowercase) from a MAC, or None.

    Accepts the same delimiters the IPAM router's ``_normalize_mac``
    accepts. Duplicated here so the service layer doesn't reach into a
    router module.
    """
    if not raw:
        return None
    cleaned = _MAC_DELIMS.sub("", raw.strip()).lower()
    if len(cleaned) < 6 or not all(c in "0123456789abcdef" for c in cleaned[:6]):
        return None
    return cleaned[:6]


async def _oui_enabled(db: AsyncSession) -> bool:
    ps = await db.get(PlatformSettings, _SINGLETON_ID)
    return bool(ps and ps.oui_lookup_enabled)


async def lookup_vendor(db: AsyncSession, mac: str | None) -> str | None:
    """Return the vendor name for one MAC, or None."""
    if not await _oui_enabled(db):
        return None
    prefix = _prefix_from_mac(mac)
    if prefix is None:
        return None
    row = await db.get(OUIVendor, prefix)
    return row.vendor_name if row else None


async def bulk_lookup_vendors(db: AsyncSession, macs: list[str | None]) -> dict[str, str]:
    """Return ``{normalized_mac: vendor_name}`` for every MAC we recognize.

    Caller-friendly shape: keyed by the *input* MAC string (trimmed +
    lowercased 12-char form used as the dict key), not by prefix. Unknown
    MACs are simply absent — makes enriching a list of rows a single
    ``.get()`` lookup. Short-circuits to ``{}`` when OUI lookup is
    disabled.
    """
    if not macs or not await _oui_enabled(db):
        return {}

    # Build {prefix -> [normalized_macs]} so a single IN query covers the page.
    by_prefix: dict[str, list[str]] = {}
    for raw in macs:
        prefix = _prefix_from_mac(raw)
        if prefix is None:
            continue
        cleaned = _MAC_DELIMS.sub("", (raw or "").strip()).lower()
        by_prefix.setdefault(prefix, []).append(cleaned)

    if not by_prefix:
        return {}

    rows = (
        await db.execute(
            select(OUIVendor.prefix, OUIVendor.vendor_name).where(
                OUIVendor.prefix.in_(list(by_prefix.keys()))
            )
        )
    ).all()

    out: dict[str, str] = {}
    for prefix, vendor in rows:
        for mac in by_prefix.get(prefix, []):
            out[mac] = vendor
    return out


def normalize_mac_key(mac: str | None) -> str | None:
    """Return the 12-char lowercase key used by :func:`bulk_lookup_vendors`.

    Exposed so callers can look up entries in the returned dict with
    exactly the same canonical form.
    """
    if not mac:
        return None
    cleaned = _MAC_DELIMS.sub("", mac.strip()).lower()
    if len(cleaned) != 12 or not all(c in "0123456789abcdef" for c in cleaned):
        return None
    return cleaned
