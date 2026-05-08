"""Multicast group registry — issue #126 Phase 1.

Tracks multicast groups as first-class IPAM entities: address +
optional ports + producer/consumer memberships. Sits inside the
existing IPSpace tree (a multicast IPSpace is one whose root CIDR
sits inside ``224.0.0.0/4`` IPv4 or ``ff00::/8`` IPv6) but the
group model is its own shape because multicast addresses denote
*streams*, not endpoints.

Phase 1 ships the registry only — manual entry, no PIM context,
no observed populators. Phase 2 adds ``MulticastDomain`` (PIM mode
+ RP + MSDP peerings) and the ``Subnet.kind`` discriminator that
forks the IPAM tree rendering. Phase 3 wires SNMP IGMP-snooping
and SAP listeners. Phase 4 adds Operator Copilot tools.

FK semantics:

* ``space_id`` is ``ON DELETE RESTRICT`` — a multicast IPSpace
  hosts its groups; losing the space silently would orphan the
  group state. Operators detach / move groups before removing
  the parent IPSpace, surfacing the dependency.
* ``vlan_id`` / ``customer_id`` / ``service_id`` are ``ON DELETE
  SET NULL`` — losing one of those tags shouldn't cascade-delete
  the group. Same shape Circuit uses for its ownership FKs.
* ``domain_id`` is a plain ``UUID`` column with no FK in Phase 1
  (the ``multicast_domain`` table lands in Phase 2). The column
  sits here so adding the FK in Phase 2 doesn't require a data
  migration.
* ``MulticastGroupPort.group_id`` and ``MulticastMembership.group_id``
  / ``ip_address_id`` are ``CASCADE`` — children only meaningful
  with a live parent.

Server-side validation:

* The ``CHECK ck_multicast_group_address_class`` constraint in the
  migration enforces that ``address`` is inside ``224.0.0.0/4``
  IPv4 or ``ff00::/8`` IPv6 — defends against a misconfigured
  client that posts a unicast IP.
* ``port_end IS NULL OR port_end >= port_start`` — NULL means
  "single port = port_start".
* ``UNIQUE (group_id, ip_address_id, role)`` on
  ``multicast_membership`` — prevents accidental duplicates from
  concurrent IGMP-snoop populators in Phase 3.
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
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Membership roles. ``rendezvous_point`` is the PIM RP for ASM
# groups; tracked here even in Phase 1 so the domain-level RP
# resolution (Phase 2) can derive RP-set membership without a
# data migration.
MEMBERSHIP_ROLES: frozenset[str] = frozenset({"producer", "consumer", "rendezvous_point"})

# How a membership row got here. ``manual`` is the operator-typed
# default; ``igmp_snooping`` and ``sap_announce`` are populated by
# observed populators in Phase 3.
MEMBERSHIP_SOURCES: frozenset[str] = frozenset({"manual", "igmp_snooping", "sap_announce"})

# Wire transports for the optional port range. Frozenset rather
# than a Postgres ENUM so adding SRT / QUIC / WebRTC later doesn't
# need a migration.
PORT_TRANSPORTS: frozenset[str] = frozenset({"udp", "rtp", "tcp", "srt"})


class MulticastGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A multicast group address — the stream identity.

    The ``(address, port)`` tuple is the real-world stream
    identity; the ports themselves live on
    ``MulticastGroupPort`` so a single group address can carry
    several flow types (e.g. SMPTE 2110 video + audio + ancillary
    on adjacent port pairs under one base address).
    """

    __tablename__ = "multicast_group"
    __table_args__ = (
        Index("ix_multicast_group_space_id", "space_id"),
        Index("ix_multicast_group_vlan_id", "vlan_id"),
        Index("ix_multicast_group_customer_id", "customer_id"),
        Index("ix_multicast_group_service_id", "service_id"),
        Index("ix_multicast_group_domain_id", "domain_id"),
        # Conformity-layer collision check (Wave 3) joins on this
        # column scoped by space.
        Index("ix_multicast_group_address", "address"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="RESTRICT"),
        nullable=False,
    )

    address: Mapped[str] = mapped_column(INET, nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Free-text application label — what's flowing on the wire.
    # ``"Cam7-Studio-B HD"`` / ``"AAPL options L2"`` / ``"Pacemaker
    # heartbeat"``. Surfaces in the list view next to the address.
    application: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )

    # RTP payload type for media flows (0-127, IANA RTP Payload
    # Types). Optional — non-RTP flows leave this NULL.
    rtp_payload_type: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Estimated bandwidth in Mbps, optional. ``Numeric(10, 3)``
    # allows fractional Mbps (1.485 Gbps SDI = 1485 Mbps; an audio
    # flow at ~1.5 Mbps still rounds cleanly) up to ~10 Tbps.
    bandwidth_mbps_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)

    vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vlan.id", ondelete="SET NULL"),
        nullable=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer.id", ondelete="SET NULL"),
        nullable=True,
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_service.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Forward placeholder — the ``multicast_domain`` table lands in
    # Phase 2, at which point the migration replaces this with a
    # proper FK. Until then it's a plain UUID column with no
    # referential integrity, so callers must treat NULL as "no
    # domain assigned" and an unknown UUID as "stale, treat as
    # NULL".
    domain_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    tags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    ports: Mapped[list[MulticastGroupPort]] = relationship(
        "MulticastGroupPort",
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    memberships: Mapped[list[MulticastMembership]] = relationship(
        "MulticastMembership",
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MulticastGroupPort(UUIDPrimaryKeyMixin, Base):
    """One port (or port range) under a multicast group.

    SMPTE 2110 video typically uses a port pair; pro-audio (Dante)
    matrices allocate a unique port per flow under a shared group
    address. ``port_end IS NULL`` means "single port =
    ``port_start``".
    """

    __tablename__ = "multicast_group_port"
    __table_args__ = (Index("ix_multicast_group_port_group_id", "group_id"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("multicast_group.id", ondelete="CASCADE"),
        nullable=False,
    )

    port_start: Mapped[int] = mapped_column(Integer, nullable=False)
    port_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Wire transport — operator-supplied. Frozenset-validated at
    # the API layer, stored as a short string. Matches the
    # Circuit.transport_class pattern.
    transport: Mapped[str] = mapped_column(String(8), nullable=False, default="udp")

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    group: Mapped[MulticastGroup] = relationship("MulticastGroup", back_populates="ports")


class MulticastMembership(UUIDPrimaryKeyMixin, Base):
    """A producer / consumer / RP relationship between an IP and a
    multicast group.

    Phase 1 only carries manual rows. Phase 3 populators
    (IGMP-snooping walks per network device, SAP announcements,
    NMOS IS-04) write rows with ``seen_via != 'manual'`` and
    stamp ``last_seen_at`` so the UI can heatmap stale entries.
    """

    __tablename__ = "multicast_membership"
    __table_args__ = (
        Index("ix_multicast_membership_group_id", "group_id"),
        Index("ix_multicast_membership_ip_address_id", "ip_address_id"),
        # Prevent dup rows from concurrent IGMP-snoop populators.
        # The same IP can hold multiple roles on a single group
        # (RP + producer is a real configuration), so role is part
        # of the key.
        UniqueConstraint(
            "group_id", "ip_address_id", "role", name="uq_multicast_membership_triplet"
        ),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("multicast_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    ip_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ``producer`` | ``consumer`` | ``rendezvous_point``
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="consumer")

    # ``manual`` | ``igmp_snooping`` | ``sap_announce``
    seen_via: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    group: Mapped[MulticastGroup] = relationship("MulticastGroup", back_populates="memberships")


__all__ = [
    "MEMBERSHIP_ROLES",
    "MEMBERSHIP_SOURCES",
    "MulticastGroup",
    "MulticastGroupPort",
    "MulticastMembership",
    "PORT_TRANSPORTS",
]
