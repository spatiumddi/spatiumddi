"""Industrial / OT network awareness — issue #542 (part of #543).

Operational-technology networks — factory floors, utilities, building
plant — run industrial Ethernet protocols (PROFINET, EtherNet/IP,
Modbus TCP, OPC UA) over IP. These devices are overwhelmingly
*statically* addressed and rarely renumbered, so the address plan is
the asset inventory, and today it lives in engineering-tool exports and
spreadsheets. SpatiumDDI is already the IP truth-source; this module
adds the OT-specific descriptor and the segmentation documentation
that make it usable as an OT system-of-record.

**Safety posture — read-only identification only.** No reading tags,
no writing coils or registers, no OPC UA subscriptions, no PLC
programming. This module stores what an operator (or, in a later
phase, a passive probe) already knows about a device. Any hint of
write access to a control protocol is out of scope, full stop — the
liability and safety argument alone settles it, and the issue says so
explicitly.

Also out of scope: deep passive DPI / anomaly detection (that is
Nozomi / Claroty / Dragos territory — we can link to them, not replace
them) and real-time determinism concerns (we can document that a
segment is TSN; we don't manage schedules).

Phase 1 (this) is the descriptor + Purdue zoning + manual entry, with
zero new network I/O. Routable discovery (EtherNet/IP List Identity,
Modbus, OPC UA) and PROFINET DCP are later phases — see
``docs/features/VERTICALS.md`` for why they aren't as cheap as
they look.

FK semantics:

* ``OTDevice.ip_address_id`` — ``ON DELETE CASCADE`` + ``UNIQUE``.
  One OT descriptor per address; the descriptor is an enrichment of
  the address and dies with it.
* ``OTZone.subnet_id`` — ``ON DELETE CASCADE`` + ``UNIQUE``. A zone
  *is* a property of the subnet; one zoning record per subnet.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Industrial protocols we classify. ``s7comm`` is Siemens' proprietary
# protocol (TCP 102) which coexists with PROFINET on the same devices;
# it stays a distinct value because an operator troubleshooting a
# Siemens cell thinks in those terms.
OT_PROTOCOLS: frozenset[str] = frozenset(
    {
        "profinet",
        "ethernet_ip",
        "modbus_tcp",
        "opc_ua",
        "s7comm",
        "bacnet_ip",
        "dnp3",
        "iec61850",
        "other",
    }
)

# Device role within the cell. Coarse on purpose — this is
# documentation for a network engineer, not an asset-management
# taxonomy. ``ews`` is an engineering workstation (the machine running
# TIA Portal / Studio 5000), which matters because it is the one node
# that legitimately talks to everything and therefore the one whose
# placement most often breaks segmentation rules.
OT_ROLES: frozenset[str] = frozenset(
    {
        "plc",
        "io_device",
        "hmi",
        "drive",
        "gateway",
        "sensor",
        "historian",
        "ews",
        "switch",
        "other",
    }
)

# How an OT device row got here. ``enip`` / ``dcp`` / ``modbus`` are
# the later discovery phases; declared now so those phases need no
# data migration.
OT_DEVICE_SOURCES: frozenset[str] = frozenset(
    {"manual", "import", "enip", "dcp", "modbus", "opcua", "profiling"}
)

# Purdue Enterprise Reference Architecture levels. Stored as Numeric
# because level 3.5 (the DMZ between the manufacturing zone and the
# enterprise) is a real, universally-used level — an integer column
# would force it to be spelled some other way, and the DMZ is exactly
# the boundary the conformity check cares most about.
PURDUE_LEVELS: frozenset[str] = frozenset({"0", "1", "2", "3", "3.5", "4", "5"})


class OTDevice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The OT identity of an IPAM address — 1:1 sidecar.

    ``profinet_device_name`` deserves its own column rather than a
    custom field because in PROFINET the device *name* — not the IP —
    is the device's identity: DCP assigns the address once, by name,
    and a replacement unit is commissioned by giving it the old name.
    An OT engineer searches by that string.
    """

    __tablename__ = "ot_device"
    __table_args__ = (
        UniqueConstraint("ip_address_id", name="uq_ot_device_ip_address"),
        Index("ix_ot_device_ot_protocol", "ot_protocol"),
        Index("ix_ot_device_ot_role", "ot_role"),
        Index("ix_ot_device_purdue_level", "purdue_level"),
    )

    ip_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="CASCADE"),
        nullable=False,
    )

    # One of ``OT_PROTOCOLS``.
    ot_protocol: Mapped[str] = mapped_column(String(24), nullable=False)

    # One of ``OT_ROLES``. Nullable — an operator often knows the
    # protocol before the role.
    ot_role: Mapped[str | None] = mapped_column(String(24), nullable=True)

    # The DCP name — PROFINET's actual device identity.
    profinet_device_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )

    ot_vendor: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    ot_product: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    ot_serial: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )
    firmware_rev: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )

    # Purdue level of the *device*, as declared by the operator. The
    # conformity check compares this against the level of the zone its
    # subnet belongs to. Numeric(2,1) covers 0 - 5 including 3.5.
    purdue_level: Mapped[Decimal | None] = mapped_column(Numeric(2, 1), nullable=True)

    # Free-text cell / area identifier (ISA-95 "work centre"). Kept as
    # text rather than an FK because plants name cells in wildly
    # local ways and forcing a registry here would be friction with no
    # payoff at Phase 1.
    cell_area: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )

    # One of ``OT_DEVICE_SOURCES``.
    seen_via: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    tags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class OTZone(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Purdue-level zoning for a subnet.

    Segmentation in OT is documentation, not a new primitive — this
    row says "subnet X is Level 2 (supervisory), cell 'Line-3 Weld'".
    That is what makes ``ot_device_crosses_purdue_boundary``
    answerable: a Level-1 PLC sitting in a Level-4 enterprise subnet
    is a finding an auditor cares about, and today nobody can produce
    that report without a spreadsheet.
    """

    __tablename__ = "ot_zone"
    __table_args__ = (
        UniqueConstraint("subnet_id", name="uq_ot_zone_subnet"),
        Index("ix_ot_zone_purdue_level", "purdue_level"),
    )

    subnet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subnet.id", ondelete="CASCADE"),
        nullable=False,
    )

    purdue_level: Mapped[Decimal] = mapped_column(Numeric(2, 1), nullable=False)

    cell_area: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    tags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


__all__ = [
    "OT_DEVICE_SOURCES",
    "OT_PROTOCOLS",
    "OT_ROLES",
    "OTDevice",
    "OTZone",
    "PURDUE_LEVELS",
]
