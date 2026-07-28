"""BACnet/IP building-automation registry services — issue #541.

SpatiumDDI documents a BACnet internetwork; it never participates in
one. Nothing in this package opens a socket, sends a ``Who-Is``, or
reads an object property — the rows it maintains are populated by the
operator (or an importer) through the REST surface, and the value it
adds is the two invariants a spreadsheet cannot enforce: device
instance numbers are unique internetwork-wide, and each IP subnet
carries exactly one BBMD.

Callers import by full path::

    from app.services.bacnet.registry import (
        DeviceInstanceConflict,
        assert_instance_available,
        next_available_instance,
        sync_subnet_id,
    )
    from app.services.bacnet.vendors import resolve_vendor_label, vendor_name_for

The agent-side ``Who-Is`` discovery sweep (Phase 2) needs a
host-networked, on-segment agent with a raw UDP broadcast socket and
an agent→subnet binding that isn't modelled today; it is deliberately
out of scope here rather than half-built.
"""

from __future__ import annotations
