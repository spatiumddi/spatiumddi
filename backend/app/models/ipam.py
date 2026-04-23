import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
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
    # Optional swatch key painted as a dot in the IPAM tree — same curated
    # palette as DNSZone.color so the two feel coherent.
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # DNS assignment — propagates down to child blocks/subnets that inherit
    dns_group_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    dns_zone_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    dns_additional_zone_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # DHCP server group — parallels the DNS inheritance path. A space-level
    # default is picked up by any child block/subnet that has
    # dhcp_inherit_settings=True and no override of its own.
    dhcp_server_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )

    # DDNS defaults — the root of the block/subnet inheritance chain.
    # Enabling ddns_enabled here and leaving descendants on inherit mode
    # cascades DDNS to every subnet in the space. Semantics mirror the
    # subnet-level fields; see ``Subnet`` for the policy enum.
    ddns_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    ddns_hostname_policy: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="client_or_generated",
        server_default=sa_text("'client_or_generated'"),
    )
    ddns_domain_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ddns_ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)

    blocks: Mapped[list["IPBlock"]] = relationship(
        "IPBlock", back_populates="space", cascade="all, delete-orphan"
    )
    subnets: Mapped[list["Subnet"]] = relationship("Subnet", back_populates="space")


class RouterZone(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Groups subnets that share a routing context (site, VRF, campus, etc.)."""

    __tablename__ = "router_zone"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # zone_type values: site | vrf_lite | mpls_domain | data_center | custom
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
    __table_args__ = (Index("ix_ip_block_network", "network"),)

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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

    # Set by the Kubernetes reconciler for pod CIDR / service CIDR
    # blocks mirrored from a cluster. FK cascades on cluster delete.
    kubernetes_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kubernetes_cluster.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Set by the Docker reconciler for wrapper blocks it creates
    # when no operator block encloses a Docker network CIDR.
    # Cascades on host delete.
    docker_host_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("docker_host.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # DNS assignment (propagates to child blocks and subnets unless overridden)
    dns_group_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    dns_zone_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    dns_additional_zone_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    dns_inherit_settings: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # DHCP assignment — same inherit-from-parent semantics as DNS. When
    # ``dhcp_inherit_settings`` is True we walk up the block chain (and
    # finally the space) for the effective server group.
    dhcp_server_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )
    dhcp_inherit_settings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # DDNS — same inherit-from-parent walk as DNS / DHCP. When
    # ``ddns_inherit_settings`` is True we walk up the block chain (and
    # finally the space) for the effective DDNS config. Override at any
    # level by flipping this toggle off and setting the four fields.
    ddns_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    ddns_hostname_policy: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="client_or_generated",
        server_default=sa_text("'client_or_generated'"),
    )
    ddns_domain_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ddns_ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ddns_inherit_settings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

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
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    block_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_block.id", ondelete="RESTRICT"), nullable=False
    )
    router_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("router_zone.id", ondelete="SET NULL"), nullable=True
    )
    vlan_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vlan.id", ondelete="SET NULL"), nullable=True, index=True
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

    # DNS assignment (mirrors ip_block fields; inherits from parent block unless overridden)
    dns_group_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    dns_zone_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    dns_additional_zone_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    dns_inherit_settings: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # DHCP assignment — same inherit-from-parent semantics as DNS.
    dhcp_server_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )
    dhcp_inherit_settings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # DDNS — dynamic DNS from DHCP leases. Independent of Kea's own DDNS
    # hook (which lives on ``DHCPScope``): this set of fields drives
    # SpatiumDDI's reconciliation layer — when a lease lands (via agent
    # push or the agentless lease pull), the DDNS service resolves a
    # hostname per ``ddns_hostname_policy`` and calls the same
    # ``_sync_dns_record`` path that static allocations use.
    #
    # Policy values:
    #   ``client_provided``      — only write if the lease has a hostname
    #   ``client_or_generated``  — use client hostname if present, else
    #                              generate ``dhcp-<hyphenated-ip>``
    #   ``always_generate``      — always synthesise, ignore client hostname
    #   ``disabled``             — never fire DDNS on this subnet
    #
    # ``ddns_enabled`` is the master toggle; the policy is only read when
    # enabled. ``ddns_ttl`` overrides the zone-level TTL for
    # auto-generated records. ``ddns_domain_override`` lets the operator
    # publish DDNS into a different zone (e.g. ``dhcp.corp.example.com``
    # rather than ``corp.example.com``) without affecting manual IPAM
    # allocations.
    ddns_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    ddns_hostname_policy: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="client_or_generated",
        server_default=sa_text("'client_or_generated'"),
    )
    ddns_domain_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ddns_ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Inherit DDNS config from the parent block chain + space. When True,
    # the four ddns_* fields above are ignored in favour of the first
    # non-inheriting ancestor's values. Default True so enabling DDNS at
    # the space level cascades automatically.
    ddns_inherit_settings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # IPv6 auto-allocation policy (ignored for IPv4 subnets — those use
    # PlatformSettings.ip_allocation_strategy which is sequential /
    # random only). Values:
    #
    #   "random"     (default) CSPRNG 64-bit host suffix + DB uniqueness
    #                retry. Right default for /64 LAN subnets where the
    #                host space is too large to sequentially enumerate.
    #   "eui64"      Derive the 64-bit host suffix from the MAC address
    #                per RFC 4291 §2.5.1 — requires a MAC on the request.
    #                Falls back to "random" if no MAC was supplied.
    #   "sequential" First-free linear scan (same 65k cap used for v4).
    #                Only sane for small v6 subnets (>= /112 or so);
    #                exposed for LAB / point-to-point /127 cases.
    ipv6_allocation_policy: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="random",
        server_default=sa_text("'random'"),
    )

    # Status: active | deprecated | reserved | quarantine
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)

    # Kubernetes integration provenance. When set, the subnet was auto-
    # created by the Kubernetes reconciler for this cluster; the FK
    # cascade-deletes mirrored subnets when the cluster row is dropped.
    kubernetes_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kubernetes_cluster.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Pod / Service CIDR subnets are routed overlays without LAN
    # semantics — no broadcast, no gateway, the full range is usable
    # pod / service IP space. Suppresses the network / broadcast /
    # gateway placeholder rows that ``POST /ipam/subnets`` normally
    # inserts, and tells the UI not to offer edit controls for those
    # LAN-specific fields.
    kubernetes_semantics: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # Docker integration provenance. Same cascade semantics as the
    # Kubernetes FK above. Docker bridge networks DO carry LAN
    # semantics (gateway + broadcast), so there's no docker_semantics
    # flag — the reconciler creates these subnets with normal LAN
    # placeholder rows.
    docker_host_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("docker_host.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Computed / cached. ``total_ips`` is BigInteger because IPv6 subnets can
    # be as large as 2^64 addresses (a /64 — the standard LAN size) which
    # overflows INT4. We still clamp at the BIGINT max (2^63 − 1) to keep
    # the math simple; utilization_percent remains a fraction and is
    # meaningless for /64s anyway.
    utilization_percent: Mapped[float] = mapped_column(nullable=False, default=0.0)
    total_ips: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    allocated_ips: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    custom_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    space: Mapped[IPSpace] = relationship("IPSpace", back_populates="subnets")
    block: Mapped[IPBlock | None] = relationship("IPBlock", back_populates="subnets")
    router_zone: Mapped[RouterZone | None] = relationship("RouterZone", back_populates="subnets")
    vlan_ref: Mapped["VLAN | None"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "VLAN", lazy="joined", foreign_keys=[vlan_ref_id]
    )
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

    # ── Linkage fields (written by Wave 3 DDNS / DHCP integration) ──
    # Forward zone hosting the A/AAAA record for this IP's FQDN.
    forward_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dns_zone.id", ondelete="SET NULL"), nullable=True
    )
    # Reverse zone hosting the PTR record for this IP.
    reverse_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dns_zone.id", ondelete="SET NULL"), nullable=True
    )
    # Linked DNS record (A/AAAA).
    dns_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dns_record.id", ondelete="SET NULL"), nullable=True
    )
    # DHCP linkage: stored as strings (DHCP models don't exist yet).
    dhcp_lease_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    static_assignment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # True if the row was auto-created from a live DHCP lease (as opposed to
    # being manually allocated or from a static assignment). When the lease
    # expires the row is removed automatically; a manually-allocated row
    # with the same IP wouldn't be.
    auto_from_lease: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    # Set by the Kubernetes reconciler when this row mirrors a k8s
    # Node or LoadBalancer Service VIP. Null on all non-k8s rows. FK
    # cascades on cluster delete so we never leak orphans.
    kubernetes_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kubernetes_cluster.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Same pattern for the Docker reconciler — set on containers
    # that are mirrored in, null otherwise.
    docker_host_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("docker_host.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    subnet: Mapped[Subnet] = relationship("Subnet", back_populates="addresses")


class SubnetDomain(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Junction table linking a Subnet to one or more DNS zones.

    The subnet keeps a convenience `dns_zone_id` pointer to its primary
    domain; the SubnetDomain rows are the source of truth for the full set.
    """

    __tablename__ = "subnet_domain"
    __table_args__ = (
        UniqueConstraint("subnet_id", "dns_zone_id", name="uq_subnet_domain_subnet_zone"),
        Index("ix_subnet_domain_subnet_id", "subnet_id"),
        Index("ix_subnet_domain_dns_zone_id", "dns_zone_id"),
    )

    subnet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnet.id", ondelete="CASCADE"), nullable=False
    )
    dns_zone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dns_zone.id", ondelete="CASCADE"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


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
    __table_args__ = (UniqueConstraint("space_id", "vlan_id", name="uq_vlan_mapping_space_vlan"),)

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vxlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
