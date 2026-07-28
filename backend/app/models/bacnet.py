"""BACnet/IP building-automation awareness — issue #541 (part of #543).

BACnet/IP is the dominant building-automation protocol (HVAC,
lighting, access control, metering). It is IP-native, so SpatiumDDI is
*almost* already its system of record — but a BACnet internetwork has
a second uniqueness namespace and one hard topology rule that no
generic IPAM tracks:

1. **Device instance numbers.** Every BACnet device carries a device
   instance number (0 - 4,194,302; 22-bit) that must be unique across
   the *entire* internetwork — not per-subnet, not per-site. Duplicate
   instances are a classic, genuinely painful BAS failure, and
   integrators track them in spreadsheets that drift. This is the same
   uniqueness-registry problem IPAM already solves for addresses,
   applied to a parallel number space, which is what makes it fit here
   rather than in a BAS head-end.

2. **Exactly one BBMD per subnet.** BACnet broadcasts (``Who-Is`` /
   ``I-Am``) don't cross IP routers, so each IP subnet in a
   multi-subnet BACnet/IP network needs exactly one BACnet Broadcast
   Management Device. Zero → devices invisible across subnets; two →
   broadcast storms and duplicate traffic. "Exactly one" is a
   naturally checkable rule, which is why it becomes a conformity
   check rather than documentation.

Out of scope, permanently: object/point-level management (present-value
reads, trend logs, schedules, alarms) — we track the *device*, not its
600 objects; MS/TP and other non-IP datalinks (serial, not
addressable); and acting as a BBMD ourselves. We document the topology,
we don't participate in it.

Phase 1 (this) is registry + conformity with zero new network I/O.
The ``Who-Is`` discovery sweep is Phase 2 and needs agent-side raw UDP
broadcast — see the module docstring note in the service layer.

FK semantics:

* ``BACnetDevice.ip_address_id`` — ``ON DELETE CASCADE``. The device
  row is an enrichment of an IPAM address; losing the address means
  losing the device's identity anchor.
* ``BACnetDevice.subnet_id`` — denormalized ``ON DELETE SET NULL``
  copy of the address's subnet. Carried explicitly so the
  ``bbmd_one_per_subnet`` check can group by subnet without a join
  through ``ip_address`` on every evaluation, and so a device whose
  address row is being re-parented doesn't silently change BBMD
  topology mid-flight.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# How a BACnet device row got here. ``whois`` is the Phase 2
# agent-side discovery sweep; ``mirror`` covers a future BAS-head-end
# import. Defined now so Phase 2 needs no data migration.
BACNET_DEVICE_SOURCES: frozenset[str] = frozenset({"manual", "whois", "mirror", "import"})

# BACnet segmentation support, as reported in an ``I-Am`` APDU.
# ASHRAE 135 enumerates exactly these four.
BACNET_SEGMENTATION: frozenset[str] = frozenset({"both", "transmit", "receive", "no-segmentation"})

# Device instance numbers are 22-bit. 4194303 (0x3FFFFF) is reserved
# by the standard to mean "unconfigured / wildcard" in ``Who-Is``
# requests, so the maximum *assignable* instance is one below it.
BACNET_INSTANCE_MIN: int = 0
BACNET_INSTANCE_MAX: int = 4194302

# BACnet network numbers are 16-bit. 0 means "local network" and
# 65535 is the broadcast network number, so neither is assignable to
# a real remote network.
BACNET_NETWORK_MIN: int = 1
BACNET_NETWORK_MAX: int = 65534

# The default BACnet/IP UDP port, 0xBAC0. Devices on plants with
# multiple logical BACnet networks over one physical LAN use
# 0xBAC1-0xBACF, so the port is stored per-device rather than assumed.
BACNET_DEFAULT_PORT: int = 47808


class BACnetDevice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One BACnet/IP device, anchored to an IPAM address.

    The globally-unique ``device_instance`` constraint is the whole
    point of this table — it is enforced in the database, not just
    validated in the API, because the failure it prevents (two devices
    answering to the same instance) is the exact thing that makes a
    BAS internetwork misbehave in ways that take days to trace.
    """

    __tablename__ = "bacnet_device"
    __table_args__ = (
        # THE constraint. Unique across the whole internetwork —
        # deliberately not scoped to space/subnet/site, because BACnet
        # instance uniqueness is internetwork-wide by specification.
        UniqueConstraint("device_instance", name="uq_bacnet_device_instance"),
        Index("ix_bacnet_device_ip_address_id", "ip_address_id"),
        Index("ix_bacnet_device_vendor_id", "vendor_id"),
        # Serves both per-subnet lookups (leading column, so the
        # single-column subnet index would be redundant) and the
        # BBMD-per-subnet conformity check.
        Index("ix_bacnet_device_subnet_bbmd", "subnet_id", "is_bbmd"),
        # The BBMD topology list filters on is_bbmd alone and orders by
        # instance. A partial index is small (BBMDs are a handful per
        # estate) and turns that page load from a seq scan into a
        # covered range scan.
        Index(
            "ix_bacnet_device_bbmd",
            "subnet_id",
            "device_instance",
            postgresql_where=text("is_bbmd"),
        ),
    )

    ip_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Denormalized from the address for cheap per-subnet grouping.
    # Kept in sync by the service layer on create/update.
    subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subnet.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 0 - 4194302, globally unique.
    device_instance: Mapped[int] = mapped_column(Integer, nullable=False)

    # ASHRAE-assigned vendor id. 0 is legitimately "ASHRAE" itself but
    # in the field a reported 0 almost always means a misconfigured or
    # cloned device, which the ``bacnet_vendor_id_unknown`` check
    # surfaces as advisory rather than failing.
    vendor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vendor_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )

    device_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    model_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    firmware_rev: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )
    location: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )

    # Max APDU length accepted, in octets, from the ``I-Am``. 1476 is
    # the BACnet/IP norm; smaller values signal a device bridged from
    # MS/TP, which is operationally useful to see.
    max_apdu: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # One of ``BACNET_SEGMENTATION``.
    segmentation_supported: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # BACnet network number this device sits on (1 - 65534).
    network_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # BACnet/IP UDP port. Defaults to 47808 (0xBAC0).
    udp_port: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=BACNET_DEFAULT_PORT,
        server_default=str(BACNET_DEFAULT_PORT),
    )

    is_bbmd: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # A device on a subnet with no local BBMD registers as a Foreign
    # Device into some BBMD's FDT. Tracking the flag separately from
    # ``is_bbmd`` because a device is one, the other, or neither.
    is_foreign_device: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Broadcast Distribution Table / Foreign Device Table snapshots,
    # only meaningful when ``is_bbmd``. JSONB rather than child tables
    # because these are read-mostly snapshots of someone else's state
    # that we never query *into* — we render them and diff them
    # wholesale. Phase 3 populates these from the wire; Phase 1
    # accepts them via API/import so a plant can document topology it
    # already knows.
    bdt: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    fdt: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    # One of ``BACNET_DEVICE_SOURCES``.
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


__all__ = [
    "BACNET_DEFAULT_PORT",
    "BACNET_DEVICE_SOURCES",
    "BACNET_INSTANCE_MAX",
    "BACNET_INSTANCE_MIN",
    "BACNET_NETWORK_MAX",
    "BACNET_NETWORK_MIN",
    "BACNET_SEGMENTATION",
    "BACnetDevice",
]
