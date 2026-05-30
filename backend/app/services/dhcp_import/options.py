"""Shared option-name maps + value helpers for the DHCP importers.

The Kea driver (``drivers/dhcp/kea.py``) and the Windows driver
(``drivers/dhcp/windows.py``) own the *forward* direction (SpatiumDDI
canonical option-name → backend wire name / option-id). The importers
need the *inverse*: backend → canonical. Rather than re-derive it three
times, the inverse maps live here, built off the same source-of-truth
constants where practical.

Canonical option names are the keys of
``drivers.dhcp.base.STANDARD_OPTION_NAMES`` (``routers`` / ``dns-servers``
/ ``domain-name`` / …). Unknown source option names are kept verbatim
(so the value round-trips through ``DHCPScope.options`` and the Kea
driver re-emits it under the same name) and recorded as a warning, never
silently dropped.
"""

from __future__ import annotations

import re
from typing import Any

from app.drivers.dhcp.kea import _KEA_OPTION_NAMES, _KEA_OPTION_NAMES_V6

# ── Kea wire name → SpatiumDDI canonical name ────────────────────────
#
# Inverse of the driver's forward maps. A given canonical name (e.g.
# ``dns-servers``) renders to ``domain-name-servers`` on the wire, so the
# inverse maps ``domain-name-servers`` back to ``dns-servers``.

_KEA_V4_NAME_TO_CANONICAL: dict[str, str] = {wire: name for name, wire in _KEA_OPTION_NAMES.items()}
_KEA_V6_NAME_TO_CANONICAL: dict[str, str] = {
    wire: name for name, wire in _KEA_OPTION_NAMES_V6.items()
}


def kea_option_to_canonical(name: str, *, address_family: str = "ipv4") -> str:
    """Map a Kea ``option-data`` name back to the SpatiumDDI canonical
    name, falling back to the name verbatim when unrecognised."""
    table = _KEA_V6_NAME_TO_CANONICAL if address_family == "ipv6" else _KEA_V4_NAME_TO_CANONICAL
    return table.get(name, name)


# ── ISC dhcpd.conf option name → SpatiumDDI canonical name ───────────
#
# ISC uses its own option vocabulary; the common subset maps cleanly.
# Anything not here is kept verbatim + warned.

_ISC_NAME_TO_CANONICAL: dict[str, str] = {
    "routers": "routers",
    "domain-name-servers": "dns-servers",
    "domain-name": "domain-name",
    "broadcast-address": "broadcast-address",
    "ntp-servers": "ntp-servers",
    "tftp-server-name": "tftp-server-name",
    "bootfile-name": "bootfile-name",
    "domain-search": "domain-search",
    "interface-mtu": "mtu",
    "time-offset": "time-offset",
}


def isc_option_to_canonical(name: str) -> str:
    """Map an ISC option name to the SpatiumDDI canonical name, falling
    back to the name verbatim when unrecognised."""
    return _ISC_NAME_TO_CANONICAL.get(name, name)


# ── MAC normalisation ────────────────────────────────────────────────
#
# Postgres MACADDR accepts colon- or dash-separated 6-byte hex. Sources
# emit a mix (Kea ``aa:bb:..``; ISC ``hardware ethernet aa:bb:..``;
# Windows ClientId ``aa-bb-..`` sometimes 7-octet with a ``01-`` hw-type
# prefix). Normalise everything to lower-case colon form.

_MAC_OCTET_RE = re.compile(r"^[0-9a-f]{2}$")


def normalise_mac(raw: str | None) -> str | None:
    """Return ``aa:bb:cc:dd:ee:ff`` (lower) or None if not a valid MAC.

    Strips a leading ``01-``/``01:`` Ethernet hardware-type prefix when
    the value has 7 octets (Windows ClientId form).
    """
    if not raw:
        return None
    parts = re.split(r"[-:]", raw.strip().lower())
    if len(parts) == 7 and parts[0] == "01":
        parts = parts[1:]
    if len(parts) != 6:
        return None
    if not all(_MAC_OCTET_RE.fullmatch(p) for p in parts):
        return None
    return ":".join(parts)


def coerce_option_value(value: Any) -> Any:
    """Normalise a parsed option value into the JSONB shape the scope
    stores. Single-element lists collapse to scalars for the options
    the Kea driver treats as scalar; everything else stays as-is."""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value
