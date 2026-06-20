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

import ipaddress
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

# Upper bound on the number of host addresses an ``explicit`` set may carry.
# The write-delegation gate (``services.ipam.address_set_gate``) re-parses every
# explicit host on EVERY IPAM mutation to build its membership set, so an
# unbounded list is a per-request DoS vector (#103, finding #6). Enforced in
# BOTH the Pydantic schemas (REST) and here (the shared validator the AI path +
# any direct caller also run through), so no surface can exceed it.
EXPLICIT_ADDRESSES_MAX: int = 1024


def validate_address_set_shape(
    range_kind: str,
    start_address: str | None,
    end_address: str | None,
    explicit_addresses: list[str],
) -> str | None:
    """Validate the contiguous/explicit shape (parse-only — no subnet check).

    Single source of truth for the contiguous/explicit + IPv4/IPv6 rules,
    shared by the REST router (``api.v1.address_sets.router``) and the AI
    operation (``services.ai.operations``) so the two surfaces can't
    diverge. Returns ``None`` when the shape is valid, otherwise an
    operator-readable error string. Each call site adapts the string into
    its own error surface (HTTP 422 vs ``PreviewResult.detail``).
    """
    if range_kind not in ADDRESS_SET_RANGE_KINDS:
        return f"range_kind must be one of {sorted(ADDRESS_SET_RANGE_KINDS)}"
    if range_kind == "contiguous":
        if not start_address or not end_address:
            return "contiguous range requires start_address and end_address"
        try:
            s = ipaddress.ip_address(start_address)
            e = ipaddress.ip_address(end_address)
        except ValueError as exc:
            return f"invalid start/end address: {exc}"
        if s.version != e.version:
            return "start_address and end_address must be the same IP family"
        if int(s) > int(e):
            return "start_address must be <= end_address"
    else:  # explicit
        if not explicit_addresses:
            return "explicit range requires a non-empty explicit_addresses list"
        if len(explicit_addresses) > EXPLICIT_ADDRESSES_MAX:
            return (
                f"explicit_addresses may not exceed {EXPLICIT_ADDRESSES_MAX} entries "
                f"(got {len(explicit_addresses)})"
            )
        for raw in explicit_addresses:
            try:
                ipaddress.ip_address(raw)
            except ValueError:
                return f"invalid address in explicit_addresses: {raw}"
    return None


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
    "EXPLICIT_ADDRESSES_MAX",
    "AddressSet",
    "validate_address_set_shape",
]
