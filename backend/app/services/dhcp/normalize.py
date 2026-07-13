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

__all__ = ["norm_ip", "norm_mac"]


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
