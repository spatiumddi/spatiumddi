"""Canonical forms for the identity fields DHCP reconcilers compare on.

A MAC read back from a Windows DHCP server (``00-15-5D-01-02-03``) and the
same MAC as Postgres stores it (``00:15:5d:01:02:03``) are the same address
written two ways. Any code that diffs wire state against DB state has to fold
both to one form first, or a cosmetic reformat reads as a change — which for
``pull_leases._upsert_scope`` means deleting a reservation and re-creating it
under a new id on every poll.

These started life as ``_norm_mac`` / ``_norm_ip`` inside
``windows_writethrough`` (#426, for its change-detection). ``pull_leases``
needs exactly the same semantics, and two definitions that could drift apart
is the last thing a reconciler wants — so they live here and both import them.
"""

from __future__ import annotations

import ipaddress
import re

__all__ = ["canonicalize_mac", "norm_ip", "norm_mac"]


_MAC_DELIMS = re.compile(r"[:\-.\s]")


def canonicalize_mac(raw: str) -> str:
    """Canonicalise an operator-entered MAC, REFUSING anything malformed.

    The strict sibling of ``norm_mac``: that one folds for comparison and
    never rejects, because a reconciler diffing wire state against the DB must
    not fail on a value the wire produced. This one validates, because the
    column is ``MACADDR`` and a typo reaching Postgres comes back to the
    operator as a 500.

    Lived in ``app/api/v1/dhcp/_mac.py`` until #972: the E911 resolver needs
    it, the DHCP config bundle imports the resolver, and an API module in that
    chain made a circular import. A pure function shared by five API routers
    and a service belongs in the service layer.
    """
    cleaned = _MAC_DELIMS.sub("", raw.strip()).lower()
    if len(cleaned) != 12 or not all(c in "0123456789abcdef" for c in cleaned):
        raise ValueError("mac_address must be 12 hex chars (any common separator allowed)")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def norm_mac(mac: str) -> str:
    """Fold a MAC to bare lowercase hex, so ``00-15-5D-…`` == ``00:15:5d:…``."""
    return "".join(c for c in mac.lower() if c in "0123456789abcdef")


def norm_ip(ip: str) -> str:
    """Canonicalise an IP for change-detection.

    Falls back to the stripped raw string when it doesn't parse, so a bad
    value still compares equal to itself rather than collapsing every
    unparseable value onto one key.
    """
    try:
        return str(ipaddress.ip_address(ip.strip()))
    except ValueError:
        return ip.strip()
