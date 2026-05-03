"""Router + VLAN models (first-class VLAN management)."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Router(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A physical or virtual L3 device that owns a set of VLANs."""

    __tablename__ = "router"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    management_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Optional BGP origin (issue #85). Stamps which AS this router
    # originates routes from. Pairs with the ``bgp_peering`` graph
    # so a router's neighbours can be derived from its ``local_asn``.
    local_asn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asn.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    vlans: Mapped[list["VLAN"]] = relationship(
        "VLAN", back_populates="router", cascade="all, delete-orphan"
    )


class VLAN(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A VLAN defined under a specific Router."""

    __tablename__ = "vlan"
    __table_args__ = (
        UniqueConstraint("router_id", "vlan_id", name="uq_vlan_router_tag"),
        UniqueConstraint("router_id", "name", name="uq_vlan_router_name"),
    )

    router_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("router.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Eager-load the parent Router so SubnetResponse.vlan.router_name can be
    # filled without an extra round-trip per subnet row.
    router: Mapped[Router] = relationship("Router", back_populates="vlans", lazy="joined")
