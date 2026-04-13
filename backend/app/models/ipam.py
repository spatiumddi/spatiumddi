import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CIDR, INET, JSONB, MACADDR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class IPSpace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """VRF / isolated routing domain. IPs in different spaces may overlap."""

    __tablename__ = "ip_space"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    blocks: Mapped[list["IPBlock"]] = relationship("IPBlock", back_populates="space", cascade="all, delete-orphan")
    subnets: Mapped[list["Subnet"]] = relationship("Subnet", back_populates="space")


class RouterZone(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Groups subnets that share a routing context (site, VRF, campus, etc.)."""

    __tablename__ = "router_zone"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # type: site | vrf_lite | mpls_domain | data_center | custom
    zone_type: Mapped[str] = mapped_column(String(50), nullable=False, default="site")
    parent_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("router_zone.id", ondelete="SET NULL"), nullable=True
    )
    contact_info: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    parent: Mapped["RouterZone | None"] = relationship("RouterZone", remote_side="RouterZone.id")
    subnets: Mapped[list["Subnet"]] = relationship("Subnet", back_populates="router_zone")


class IPBlock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """
    Aggregate/supernet range for organizational grouping.
    IPs are not directly assigned to blocks — only to subnets.
    Blocks can be nested (parent_block_id).
    """

    __tablename__ = "ip_block"
    __table_args__ = (
        Index("ix_ip_block_network", "network"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_space.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_block_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_block.id", ondelete="CASCADE"), nullable=True
    )

    network: Mapped[str] = mapped_column(CIDR, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Computed / cached
    utilization_percent: Mapped[float] = mapped_column(nullable=False, default=0.0)

    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    custom_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    space: Mapped[IPSpace] = relationship("IPSpace", back_populates="blocks")
    parent: Mapped["IPBlock | None"] = relationship("IPBlock", remote_side="IPBlock.id")
    children: Mapped[list["IPBlock"]] = relationship("IPBlock", back_populates="parent")
    subnets: Mapped[list["Subnet"]] = relationship("Subnet", back_populates="block")


class Subnet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Primary managed unit. Routable network; IPs are allocated here."""

    __tablename__ = "subnet"
    __table_args__ = (
        Index("ix_subnet_network", "network"),
        # Subnets cannot overlap within the same IP space (enforced at application layer)
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_space.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    block_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_block.id", ondelete="SET NULL"), nullable=True
    )
    router_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("router_zone.id", ondelete="SET NULL"), nullable=True
    )

    network: Mapped[str] = mapped_column(CIDR, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Layer 2
    vlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vxlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Routing
    gateway: Mapped[str | None] = mapped_column(INET, nullable=True)

    # DNS integration (FK to DNS models added in Phase 2)
    dns_servers: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    domain_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # NTP
    ntp_servers: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # Status: active | deprecated | reserved | quarantine
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)

    # Computed / cached
    utilization_percent: Mapped[float] = mapped_column(nullable=False, default=0.0)
    total_ips: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allocated_ips: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    custom_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    space: Mapped[IPSpace] = relationship("IPSpace", back_populates="subnets")
    block: Mapped[IPBlock | None] = relationship("IPBlock", back_populates="subnets")
    router_zone: Mapped[RouterZone | None] = relationship("RouterZone", back_populates="subnets")
    addresses: Mapped[list["IPAddress"]] = relationship(
        "IPAddress", back_populates="subnet", cascade="all, delete-orphan"
    )


class IPAddress(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Individual IP address within a subnet."""

    __tablename__ = "ip_address"
    __table_args__ = (
        Index("ix_ip_address_address", "address"),
        UniqueConstraint("subnet_id", "address", name="uq_ip_address_subnet_address"),
    )

    subnet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnet.id", ondelete="CASCADE"), nullable=False, index=True
    )

    address: Mapped[str] = mapped_column(INET, nullable=False)

    # status: available | allocated | reserved | dhcp | static_dhcp |
    #          discovered | orphan | deprecated
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="available", index=True)

    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    fqdn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mac_address: Mapped[str | None] = mapped_column(MACADDR, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Ownership
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    owner_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group.id", ondelete="SET NULL"), nullable=True
    )
    managed_by: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Discovery
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # last_seen_method: ping | arp | dhcp | manual | snmp
    last_seen_method: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    custom_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    subnet: Mapped[Subnet] = relationship("Subnet", back_populates="addresses")


class CustomFieldDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Administrator-defined metadata fields attached to IPAM and DNS resources."""

    __tablename__ = "custom_field_definition"
    __table_args__ = (
        UniqueConstraint("resource_type", "name", name="uq_custom_field_resource_name"),
    )

    # resource_type: subnet | ip_address | ip_block | dns_zone | dhcp_scope
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # field_type: text | number | boolean | date | email | url | select | multi_select
    field_type: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    options: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # for select types
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class VLANMapping(UUIDPrimaryKeyMixin, Base):
    """Reference table tracking VLAN → VXLAN mappings."""

    __tablename__ = "vlan_mapping"
    __table_args__ = (
        UniqueConstraint("space_id", "vlan_id", name="uq_vlan_mapping_space_vlan"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_space.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vxlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
