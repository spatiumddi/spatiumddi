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

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Valid IPAddress.status values. Operator-settable values cover the
# manual lifecycle (free → allocated → reserved → deprecated, plus the
# static_dhcp tag for DHCP reservations). Integration-owned values are
# stamped by reconcilers (Proxmox / Docker / Kubernetes / DHCP lease
# pull). The combined set is what API validators accept on update;
# operators can keep an integration status as-is on a row, or override
# with any operator-settable value (which stamps user_modified_at on
# the row and locks it from future reconciler overwrites).
IP_STATUSES_OPERATOR_SETTABLE: frozenset[str] = frozenset(
    {
        "available",
        "allocated",
        "reserved",
        "static_dhcp",
        "deprecated",
        "orphan",
    }
)
IP_STATUSES_INTEGRATION_OWNED: frozenset[str] = frozenset(
    {
        "dhcp",
        "docker-container",
        "kubernetes-node",
        "kubernetes-lb",
        "kubernetes-service",
        "proxmox-vm",
        "proxmox-lxc",
        "tailscale-node",
        # Stamped by the nmap "Stamp alive hosts → IPAM" action on a
        # multi-host (CIDR) scan result. Marks an IP as "we saw a host
        # on the wire here" without claiming it's allocated. Operators
        # can transition discovered rows to allocated / reserved
        # through the normal edit path; that stamps user_modified_at
        # and locks the row from further auto-discovery overwrites.
        "discovered",
    }
)
IP_STATUSES: frozenset[str] = IP_STATUSES_OPERATOR_SETTABLE | IP_STATUSES_INTEGRATION_OWNED

# Valid IPAddress.role values. Orthogonal to ``status`` — the role
# describes what the IP *is* (a host vs. a VRRP virtual address),
# while the status describes its lifecycle (allocated vs. reserved).
# Roles in ``IP_ROLES_SHARED`` (anycast / vip / vrrp) are intentionally
# shared across multiple devices and bypass the MAC-collision warning.
IP_ROLES: frozenset[str] = frozenset(
    {"host", "loopback", "anycast", "vip", "vrrp", "secondary", "gateway"}
)
IP_ROLES_SHARED: frozenset[str] = frozenset({"anycast", "vip", "vrrp"})


class IPSpace(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
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

    # ── VRF / routing annotation ─────────────────────────────────────────
    # Pure metadata. Address-overlap semantics for VRFs are already
    # handled by having one IPSpace per VRF (overlapping IPs live in
    # separate IPSpace rows). These columns let the operator record the
    # canonical VRF name + RD + RT(s) for documentation / change
    # management; address allocation does not consult them.
    #
    # ``route_distinguisher`` is conventionally the ASN:idx
    # (``65000:100``) or IPv4:idx (``192.0.2.1:1``) form; stored as
    # plain text since vendor opinions on the canonical form differ
    # and validation churn buys nothing here.
    #
    # ``route_targets`` is a JSONB array of strings to keep room for
    # the inline "import:A:B; export:C:D" convention; first-class
    # import / export columns can be split out later without breaking
    # the data shape.
    vrf_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_distinguisher: Mapped[str | None] = mapped_column(String(32), nullable=True)
    route_targets: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

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


class IPBlock(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
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
    # Set by the Proxmox reconciler for blocks it creates from bridge
    # CIDRs. Cascades on endpoint delete.
    proxmox_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxmox_node.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Set by the Tailscale reconciler for the auto-created CGNAT +
    # IPv6 ULA blocks under the bound space. Cascades on tenant
    # delete.
    tailscale_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tailscale_tenant.id", ondelete="CASCADE"),
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


class Subnet(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
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

    # ── Device profiling — auto-nmap on new DHCP leases ──────────────
    # When enabled, a fresh DHCP lease landing in this subnet enqueues
    # an active nmap scan against the leased IP via the existing
    # NmapScan pipeline. ``auto_profile_preset`` picks one of the
    # PRESETS keys in services/nmap/runner.py (default
    # ``service_version`` — gives OS hints + open services without the
    # heavyweight ``aggressive`` flag). ``auto_profile_refresh_days``
    # is the dedupe window: the same (mac, ip) pair won't re-scan
    # within this many days of its last profile, so churning Wi-Fi
    # leases don't spawn back-to-back scans.
    #
    # Tradeoffs intentionally exposed in the UI: nmap from the
    # SpatiumDDI host shows up as a port-scan to corporate IDS, so
    # the toggle is default-off and operators have to opt in per
    # subnet. See CLAUDE.md "Device profiling" entry for the full
    # design. (Block/space inheritance intentionally omitted in Phase 1 —
    # added once operators ask for cascade.)
    auto_profile_on_dhcp_lease: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    auto_profile_preset: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="service_and_os",
        server_default=sa_text("'service_and_os'"),
    )
    auto_profile_refresh_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default=sa_text("30")
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
    # Proxmox integration provenance. Bridges + VLAN interfaces on a
    # PVE node carry a real LAN CIDR (gateway + broadcast) so normal
    # placeholder rows apply, same as Docker.
    proxmox_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxmox_node.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Tailscale integration provenance. Subnets created by the
    # Tailscale reconciler under the auto-created CGNAT + IPv6 ULA
    # blocks. Tailnet IPs are routed overlays without LAN semantics
    # (no broadcast, no gateway), so the reconciler sets this with
    # ``kubernetes_semantics=True`` analogue logic — but we don't
    # need a separate flag because we don't allocate sub-subnets in
    # the CGNAT block; every device IP lives in one big subnet.
    tailscale_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tailscale_tenant.id", ondelete="CASCADE"),
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

    # Role describes what the IP *is*, orthogonal to status. Allowed values
    # live in ``IP_ROLES``; null means uncategorised. Roles in
    # ``IP_ROLES_SHARED`` bypass MAC-collision warnings (intentional sharing).
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Reservation TTL — only meaningful when status='reserved'.
    # The sweep_expired_reservations beat task flips expired rows to
    # 'available' and clears this column.  Null = indefinite reservation.
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    # Proxmox provenance — set on rows mirrored from a PVE VM or LXC.
    # Cascades on endpoint delete.
    proxmox_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proxmox_node.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Tailscale provenance — set on rows mirrored from a tailnet
    # device. Cascades on tenant delete.
    tailscale_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tailscale_tenant.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # ── Device profiling — populated by the auto-profile pipeline ────
    # ``last_profiled_at`` is the timestamp of the last *successful*
    # profile scan; the dedupe gate in services/profiling/auto_profile.py
    # uses it together with the subnet's ``auto_profile_refresh_days``
    # to decide whether a fresh lease should re-trigger a scan.
    # ``last_profile_scan_id`` deep-links to the NmapScan row that
    # produced the most recent result, so the IP detail modal can
    # render the full scan output without denormalising it here.
    last_profiled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_profile_scan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nmap_scan.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Device profile — passive layer (Phase 2) ─────────────────────
    # Denormalised fingerbank lookup result for fast list rendering.
    # Source of truth is ``dhcp_fingerprint`` keyed by MAC; the
    # fingerprint task stamps these three columns whenever a fresh
    # lookup lands. Operators with edits on the row (``user_modified_at``
    # not null) keep their values — the stamper respects the same
    # lock the integration reconcilers use. nmap OS-detection
    # populates the same surface (``os.name`` → ``device_type``) so
    # the IP detail modal can render one consistent "device" line
    # whether enrichment came from passive DHCP or active nmap.
    device_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    device_class: Mapped[str | None] = mapped_column(String(50), nullable=True)
    device_manufacturer: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Stamped by the API write path when an operator edits the row's
    # soft fields (hostname / description / status / mac_address).
    # While non-null, integration reconcilers (Proxmox / Docker /
    # Kubernetes) treat those soft fields as locked and skip overwrites
    # — so a VM rename in PVE doesn't blow away an operator-chosen IPAM
    # name, and a row that pre-existed before the integration was
    # enabled keeps its operator-chosen values when the reconciler
    # claims it. ``subnet_id`` is always updated regardless of the lock
    # — that's a factual binding between address and subnet, not an
    # operator preference. Cleared by the reconciler when the row is
    # un-claimed (integration-owned but no longer in the upstream
    # snapshot, with the operator's edits preserved).
    user_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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


class IpMacHistory(UUIDPrimaryKeyMixin, Base):
    """Per-IP MAC observation log (upsert on every create/update with a MAC).

    Keyed on (ip_address_id, mac_address); last_seen bumped each touch.
    Cascades on IP delete.
    """

    __tablename__ = "ip_mac_history"
    __table_args__ = (
        UniqueConstraint("ip_address_id", "mac_address", name="uq_ip_mac_history_ip_mac"),
        Index("ix_ip_mac_history_ip_address_id", "ip_address_id"),
        Index("ix_ip_mac_history_last_seen", "last_seen"),
    )

    ip_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="CASCADE"),
        nullable=False,
    )
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


class NATMapping(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Operator-curated NAT/PAT mapping for IPAM cross-reference.

    Three kinds: ``1to1`` (static NAT), ``pat`` (port translation),
    ``hide`` (many-to-one masquerade). SpatiumDDI records but does not
    push these rules; they surface as a nat_mapping_count badge on IP rows.

    Both internal and external IPs carry an optional FK to ``ip_address``
    in addition to the raw INET string. The FK is auto-resolved on
    write when the typed string matches an existing IPAM row; the
    string stays authoritative so external IPs not tracked in IPAM
    (e.g. a public WAN address) still work. ``ON DELETE SET NULL``
    so deleting an IPAM row leaves the NAT history intact.
    """

    __tablename__ = "nat_mapping"
    __table_args__ = (
        Index("ix_nat_mapping_internal_ip", "internal_ip"),
        Index("ix_nat_mapping_external_ip", "external_ip"),
        Index("ix_nat_mapping_internal_subnet_id", "internal_subnet_id"),
        Index("ix_nat_mapping_internal_ip_address_id", "internal_ip_address_id"),
        Index("ix_nat_mapping_external_ip_address_id", "external_ip_address_id"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # 1to1 | pat | hide

    internal_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    internal_ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_address.id", ondelete="SET NULL"), nullable=True
    )
    internal_subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnet.id", ondelete="SET NULL"), nullable=True
    )
    internal_port_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    internal_port_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    external_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    external_ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_address.id", ondelete="SET NULL"), nullable=True
    )
    external_port_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_port_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    protocol: Mapped[str] = mapped_column(String(10), nullable=False, default="any")
    device_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    custom_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


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


class SubnetPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Operator-designed multi-level CIDR plan, applied transactionally.

    The ``tree`` JSONB carries the planned hierarchy. Each node has the
    shape::

        {
            "id":          "node-uuid",          // client-stable id (DnD)
            "network":     "10.0.0.0/16",        // CIDR
            "name":        "Datacenter East",    // operator-friendly label
            "description": "...",
            "children":    [ ... ],              // nested nodes
        }

    The root node may carry ``"existing_block_id": "<uuid>"`` to anchor the
    plan inside an already-existing IPBlock. When set, apply does not
    re-create the root — only the descendants are materialised, with the
    existing block as their parent. When null, the root itself becomes a
    new top-level block in the bound IPSpace on apply.

    A node with children = an IPBlock; a leaf = a Subnet. The kind is
    inferred from tree shape so the operator doesn't have to toggle a
    per-node flag.

    ``applied_at`` is set the first time the plan is applied; once
    applied, the plan is immutable (the materialised IPAM rows are the
    source of truth). ``applied_resource_ids`` records what was
    created so operators can audit "this plan produced these blocks +
    subnets".
    """

    __tablename__ = "subnet_plan"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tree: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_resource_ids: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    space: Mapped["IPSpace"] = relationship("IPSpace")
