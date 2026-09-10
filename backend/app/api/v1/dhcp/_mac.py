"""Shared MAC canonicalization for the DHCP endpoints.

The implementation moved to ``app.services.dhcp.normalize`` in #972: the E911
resolver needs it, the DHCP config bundle imports that resolver, and an
``app.api`` module in that chain is a circular import. Re-exported here so the
five routers that import from this path keep working and there is still
exactly one implementation.
"""

from __future__ import annotations

from app.services.dhcp.normalize import canonicalize_mac

__all__ = ["canonicalize_mac"]
