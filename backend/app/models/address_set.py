"""Address sets — named IP ranges within a subnet with their own RBAC scope (#103).

An ``AddressSet`` is a named slice of a subnet (a contiguous range such
as ``.50``–``.99``, or an explicit list of host addresses) that carries
its own ``resource_type="address_set"`` identity in the RBAC permission
grammar. The point is *delegation*: granting ``write``/``admin`` on a
single address-set id lets a department admin edit just their slice of
a subnet's address space without holding subnet-wide write.

The gate lives in the IPAM address handlers (``app.api.v1.ipam.router``):
a mutation against an IP is permitted when the caller holds subnet
``write`` **or** holds ``write``/``admin`` on some address set whose
range covers that IP. The set rows themselves are CRUD-managed through
``/api/v1/address-sets`` and gated behind the ``ipam.address_sets``
feature module (non-negotiable #14).

``customer_id`` / ``site_id`` are the logical-ownership cross-references
(#91), ``ON DELETE SET NULL`` so deleting an owner re-tags rather than
cascades. ``subnet_id`` is ``ON DELETE CASCADE`` — a set has no meaning
once its subnet is gone.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# ``contiguous`` — a ``start_address``..``end_address`` span.
# ``explicit``   — an arbitrary list of host addresses in ``explicit_addresses``.
ADDRESS_SET_RANGE_KINDS: frozenset[str] = frozenset({"contiguous", "explicit"})


class AddressSet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named, RBAC-scoped slice of a subnet's address space."""

    __tablename__ = "address_set"
    __table_args__ = (
        UniqueConstraint("subnet_id", "name", name="uq_address_set_subnet_name"),
        # ``start_address <= end_address`` whenever both are set. NULL-safe
        # so explicit-kind sets (no contiguous bounds) pass.
        CheckConstraint(
            "end_address IS NULL OR start_address <= end_address",
            name="ck_address_set_range_order",
        ),
        Index("ix_address_set_subnet_id", "subnet_id"),
        Index("ix_address_set_customer_id", "customer_id"),
        Index("ix_address_set_site_id", "site_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    # Indexes are declared once in ``__table_args__`` (matching the
    # migration's index set), so the columns themselves don't carry
    # ``index=True`` — that would emit a second redundant index.
    subnet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subnet.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Logical-ownership cross-references (#91). SET NULL so deleting an
    # owner re-tags the set rather than cascading the set delete.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer.id", ondelete="SET NULL"),
        nullable=True,
    )
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("site.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ``contiguous`` | ``explicit``
    range_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="contiguous", server_default="contiguous"
    )
    # Inclusive bounds for ``contiguous`` sets; NULL for ``explicit``.
    start_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    end_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    # Host addresses for ``explicit`` sets (list of string IPs).
    explicit_addresses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    tags: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    custom_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    subnet = relationship("Subnet")


__all__ = [
    "ADDRESS_SET_RANGE_KINDS",
    "AddressSet",
]
