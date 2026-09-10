"""Shared MAC canonicalization for the DHCP endpoints.

The implementation moved to ``app.core.mac`` in #972 — see that module for why
both earlier homes created import cycles. Re-exported here so the five routers
that import from this path keep working and there is still exactly one
implementation.
"""

from __future__ import annotations

from app.core.mac import canonicalize_mac

__all__ = ["canonicalize_mac"]
