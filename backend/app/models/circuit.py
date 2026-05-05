"""WAN circuits — physical-pipe tracking (issue #93).

A ``Circuit`` is the carrier-supplied logical pipe (the contract +
transport class + bandwidth + endpoints), distinct from the equipment
that lights it up (router serial numbers, ONT IDs, fiber strands —
that's CMDB territory, intentionally out of scope).

Foundation for the future MPLS L3VPN service catalog (issue #94) and
SD-WAN overlay routing (issue #95) — both reference circuits by
``transport_class``.

FK semantics:

* ``provider_id`` is ``ON DELETE RESTRICT`` — required, and the
  carrier relationship is too load-bearing to silently null out.
  Operators have to detach / re-provider circuits before the upstream
  Provider row can be removed, surfacing the dependency.
* All other FKs (``customer_id``, the four endpoint refs) are
  ``ON DELETE SET NULL`` — losing a Site / Customer / Subnet
  shouldn't cascade-delete the circuit, just orphan the binding so an
  operator can re-attach.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Carrier transport classes. The list is operator-facing — the
# SD-WAN overlay roadmap (#95) will reference circuits by class to
# drive routing policies ("prefer mpls for voice, prefer
# internet_broadband for backup"). Cloud cross-connect entries
# (direct_connect_aws / express_route_azure / interconnect_gcp) are
# called out separately because their pricing model + termination UX
# differ from carrier dark fiber / lambda offerings.
TRANSPORT_CLASSES: frozenset[str] = frozenset(
    {
        "mpls",
        "internet_broadband",
        "fiber_direct",
        "wavelength",
        "lte",
        "satellite",
        "direct_connect_aws",
        "express_route_azure",
        "interconnect_gcp",
    }
)

CIRCUIT_STATUSES: frozenset[str] = frozenset({"active", "pending", "suspended", "decom"})


class Circuit(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A carrier-supplied WAN circuit — the logical contract, not the
    equipment lighting it up.

    Soft-deletable: ``status="decom"`` is the operator-visible
    end-of-life flag, but operators commonly want to restore a
    decommissioned circuit when answering "what carrier did Site-X use
    in 2024?". The trash-restore flow uses the same shared
    ``deleted_at`` window.
    """

    __tablename__ = "circuit"
    __table_args__ = (
        Index("ix_circuit_provider_id", "provider_id"),
        Index("ix_circuit_customer_id", "customer_id"),
        Index("ix_circuit_a_end_site_id", "a_end_site_id"),
        Index("ix_circuit_z_end_site_id", "z_end_site_id"),
        Index("ix_circuit_a_end_subnet_id", "a_end_subnet_id"),
        Index("ix_circuit_z_end_subnet_id", "z_end_subnet_id"),
        Index("ix_circuit_transport_class", "transport_class"),
        Index("ix_circuit_status", "status"),
        Index("ix_circuit_term_end_date", "term_end_date"),
        Index("ix_circuit_ckt_id", "ckt_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Carrier-supplied identifier. Non-unique because carriers
    # occasionally reuse IDs and operators need to be able to enter
    # whatever string the carrier hands them.
    ckt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider.id", ondelete="RESTRICT"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ``mpls`` | ``internet_broadband`` | ``fiber_direct`` |
    # ``wavelength`` | ``lte`` | ``satellite`` |
    # ``direct_connect_aws`` | ``express_route_azure`` | ``interconnect_gcp``
    transport_class: Mapped[str] = mapped_column(
        String(32), nullable=False, default="internet_broadband"
    )

    # Asymmetric bandwidth supported. Mbps fits comfortably in INT4
    # for any current carrier offer (100 Gbps == 100_000 Mbps).
    bandwidth_mbps_down: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bandwidth_mbps_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Endpoints. A circuit can span site→site (typical WAN), site→cloud
    # (cross-connect), or be incomplete during commissioning.
    a_end_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("site.id", ondelete="SET NULL"),
        nullable=True,
    )
    a_end_subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subnet.id", ondelete="SET NULL"),
        nullable=True,
    )
    z_end_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("site.id", ondelete="SET NULL"),
        nullable=True,
    )
    z_end_subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subnet.id", ondelete="SET NULL"),
        nullable=True,
    )

    term_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    term_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Operator-supplied monthly cost. Numeric(10,2) gives 8 digits of
    # whole-dollar room which is more than enough for any single
    # circuit. Currency is a 3-char ISO 4217 code; we don't try to
    # convert across currencies — reporting groups by ``currency`` and
    # sums per-currency only.
    monthly_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD", server_default="USD"
    )

    # ``active`` | ``pending`` | ``suspended`` | ``decom``
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    custom_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # Snapshot of the previous ``status`` value for the
    # ``circuit_status_changed`` alert rule (issue #93 deferred
    # follow-up). Stamped on every update where status actually
    # changed; alert rule reads this column to know the transition
    # direction. Nullable until the first transition so existing rows
    # don't trigger spurious alerts on day one.
    previous_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_status_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["Circuit", "TRANSPORT_CLASSES", "CIRCUIT_STATUSES"]
