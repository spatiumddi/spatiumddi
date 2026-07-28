"""Industrial / OT network awareness service layer — issue #542 (part of #543).

**Safety posture: read-only identification only.** Nothing in this
package opens a socket. There is no Modbus read, no CIP List Identity,
no OPC UA session, no PROFINET DCP frame — and there will never be a
control-protocol *write*. Every function here is either a pure
transformation over data the caller already holds, or a plain database
read. That is the whole design constraint of #542.

Modules (imported by full path, per the ``services/netbird`` layout)::

    from app.services.ot.importer import parse_rows, plan_import, apply_plan
    from app.services.ot.zones import effective_zone_for_address, purdue_mismatch

``zones``     — the subnet → :class:`~app.models.ot.OTZone` lookup plus
                the Purdue boundary comparison the conformity rule and
                the by-address descriptor both need.
``importer``  — CSV ingest of an engineering-tool export (TIA Portal /
                Studio 5000 style) as a preview → commit pair. It
                enriches IPAM rows that already exist; it never invents
                addresses.
"""

from __future__ import annotations
