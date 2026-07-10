"""NetBird integration — read-only mirror of NetBird peers into IPAM.

NetBird is a managed WireGuard mesh overlay (self-hostable or cloud).
This package mirrors its peer inventory into the bound IPAM space and,
optionally (Phase 2), synthesises the mesh's DNS domain as a read-only
zone. SpatiumDDI never writes to NetBird.

Callers import by full path::

    from app.services.netbird.client import NetbirdClient, NetbirdClientError
    from app.services.netbird.reconcile import reconcile_instance
"""

from __future__ import annotations
