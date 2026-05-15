"""DHCP data models — server groups, servers, scopes, pools, static
assignments, client classes, leases, and agent op queue.

Configuration lives on **DHCPServerGroup**: all servers in a group
serve the same scopes / pools / statics / client classes. A group
with a single Kea server is a standalone DHCP service; a group with
two Kea servers is implicitly an HA pair, using the group's mode +
tuning fields to drive the ``libdhcp_ha.so`` hook.

Per-server fields stay on **DHCPServer**: registration + agent state,
health, and the server's own HA peer URL (the listener endpoint the
partner calls). Leases are per-server — each Kea owns its own
memfile — and the ops queue is per-server.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import INET, JSONB, MACADDR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# ── Server Group / Server ────────────────────────────────────────────────────


class DHCPServerGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Logical cluster of DHCP servers. Primary configuration container.

    All servers in a group render identical config bundles (except
    ``this-server-name`` under Kea HA). A group with two Kea members
    is an HA pair; the group's ``mode`` + HA tuning drive the
    ``libdhcp_ha.so`` hook. A single-member group is standalone and
    ignores HA fields.
    """

    __tablename__ = "dhcp_server_group"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # mode: load-balancing | hot-standby (only rendered when the group has >= 2 Kea peers)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="hot-standby")

    # #170 Wave C2 — appliance-side container networking mode. The
    # supervisor reads this off the heartbeat response when rendering
    # the dhcp-kea compose snippet on a host with the ``dhcp`` role
    # assigned. Two values:
    #   * ``host`` — container shares the host's network namespace
    #     (today's behaviour). Required for receiving raw L2
    #     broadcasts from clients on the same broadcast domain.
    #   * ``bridged`` — container listens on the host IP UDP/67 only.
    #     For deployments where the DHCP server sits behind a relay
    #     (``ip helper-address`` / ``dhcrelay``) or a DMZ NAT. Does
    #     NOT receive local L2 broadcasts.
    network_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="host", server_default="host"
    )

    # Kea HA hook tuning — rendered into libdhcp_ha.so config when the
    # group is an HA pair. Defaults mirror Kea's documented recommendations.
    heartbeat_delay_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    max_response_delay_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=60000)
    max_ack_delay_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    max_unacked_clients: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    auto_failover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Eager-load `servers` by default. The API's group list endpoint
    # reads this relationship to compute `kea_member_count` + roll up
    # the members, and it runs in an async session where an accidental
    # sync lazy-load crashes with MissingGreenlet. selectin is one
    # extra small query per list call; not worth the footgun.
    servers: Mapped[list[DHCPServer]] = relationship(
        "DHCPServer",
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    scopes: Mapped[list[DHCPScope]] = relationship(
        "DHCPScope", back_populates="group", cascade="all, delete-orphan"
    )
    client_classes: Mapped[list[DHCPClientClass]] = relationship(
        "DHCPClientClass", back_populates="group", cascade="all, delete-orphan"
    )
    mac_blocks: Mapped[list[DHCPMACBlock]] = relationship(
        "DHCPMACBlock", back_populates="group", cascade="all, delete-orphan"
    )
    option_templates: Mapped[list[DHCPOptionTemplate]] = relationship(
        "DHCPOptionTemplate", back_populates="group", cascade="all, delete-orphan"
    )


class DHCPServer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Individual DHCP server (Kea instance or Windows DHCP) in a group."""

    __tablename__ = "dhcp_server"
    __table_args__ = (
        UniqueConstraint("name", name="uq_dhcp_server_name"),
        Index("ix_dhcp_server_agent_id", "agent_id", unique=True),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # driver: kea | windows_dhcp
    driver: Mapped[str] = mapped_column(String(50), nullable=False, default="kea")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=67)
    # roles: primary | secondary | standalone (JSON array of strings — informational)
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: [])

    server_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # status: active | unreachable | syncing | error | pending
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Agent bookkeeping (mirrors DNSServer — see docs/deployment/DNS_AGENT.md)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    agent_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Source IP of the most recent agent heartbeat — operator-visible
    # to identify which host runs each agent in NAT / distributed
    # deployments. See dns_server.last_seen_ip for the same field.
    last_seen_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    agent_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Phase 8f fleet upgrade orchestration (issue #138). Mirror of the
    # ``DNSServer`` columns — same schema because both server kinds
    # share the agent bookkeeping shape and Fleet view treats them
    # uniformly. See DNSServer for the per-field description.
    desired_appliance_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_slot_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    deployment_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    installed_appliance_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_slot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    durable_default: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_trial_boot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_upgrade_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_upgrade_state_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 8f-8 — operator-triggered reboot. See DNSServer for full
    # rationale; same schema both sides for uniform Fleet handling.
    reboot_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reboot_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Fernet-encrypted JSON blob for driver-specific admin credentials.
    # windows_dhcp stores a dict: {"username", "password", "winrm_port",
    # "transport", "use_tls", "verify_tls"}. Agent-based drivers (kea)
    # leave this NULL — they authenticate via agent JWT.
    credentials_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Kea HA hook peer URL — this server's OWN HA listener endpoint
    # (``http://<host>:<port>/``). The other peer in the group calls
    # this URL for heartbeats / lease updates. Empty string for
    # standalone servers; rendered into every peer's ``peers`` array
    # so they know where to reach each other.
    ha_peer_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    # Kea HA runtime state — populated by the agent's periodic
    # ``status-get`` poll. Null when the server is standalone. Values
    # follow Kea's own state names (``hot-standby`` / ``normal`` /
    # ``partner-down`` / etc). Treat as opaque reporting.
    ha_state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ha_last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    group: Mapped[DHCPServerGroup | None] = relationship(
        "DHCPServerGroup", back_populates="servers", lazy="joined"
    )
    leases: Mapped[list[DHCPLease]] = relationship(
        "DHCPLease", back_populates="server", cascade="all, delete-orphan"
    )


# ── Scope / Pool / Static / Client Class ─────────────────────────────────────


class DHCPScope(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A DHCP scope — one subnet served by one group.

    Under the group-centric model, a scope belongs to a DHCPServerGroup,
    not a single server. All servers in the group render the same scope
    in their Kea config (Dhcp4 ``subnet4``). This mirrors what Kea HA
    requires and replaces the pre-2026.04.22 per-server scope rows that
    operators had to mirror manually.
    """

    __tablename__ = "dhcp_scope"
    __table_args__ = (
        UniqueConstraint("group_id", "subnet_id", name="uq_dhcp_scope_group_subnet"),
        Index("ix_dhcp_scope_group", "group_id"),
        Index("ix_dhcp_scope_subnet", "subnet_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    subnet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subnet.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Human label for the scope (optional, not unique).
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Address family: "ipv4" (Dhcp4) or "ipv6" (Dhcp6). Populated from the
    # bound subnet's prefix at create time.
    address_family: Mapped[str] = mapped_column(
        String(4), nullable=False, default="ipv4", server_default="ipv4"
    )

    lease_time: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    min_lease_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_lease_time: Mapped[int | None] = mapped_column(Integer, nullable=True)

    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {})

    # DDNS
    ddns_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ddns_hostname_policy: Mapped[str] = mapped_column(String(30), nullable=False, default="client")
    hostname_to_ipam_sync: Mapped[str] = mapped_column(
        String(30), nullable=False, default="on_static_only"
    )

    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── PXE / iPXE provisioning (issue #51) ─────────────────────────
    # Operator picks one PXEProfile per scope. The profile carries
    # the next-server + N arch-matches; the Kea driver renders one
    # client-class per (profile × arch-match) pair when this is set.
    # Null = no PXE on this scope (default — most scopes don't run
    # PXE; a single scope that does avoids polluting every scope's
    # config). FK is SET NULL so deleting a profile doesn't cascade-
    # trash the scope; the scope just stops emitting PXE classes.
    pxe_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_pxe_profile.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Free-form ``key → value`` labels (issue #104).
    tags: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )

    group: Mapped[DHCPServerGroup] = relationship("DHCPServerGroup", back_populates="scopes")
    pools: Mapped[list[DHCPPool]] = relationship(
        "DHCPPool",
        back_populates="scope",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    statics: Mapped[list[DHCPStaticAssignment]] = relationship(
        "DHCPStaticAssignment",
        back_populates="scope",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    # ``selectin`` rather than ``joined`` because the profile's
    # ``matches`` collection is itself joined-loaded — pulling profile
    # via JOIN on scope queries pulls a JOIN-against-collection that
    # SQLAlchemy refuses without ``.unique()``. Selectin issues one
    # extra small query keyed by ``pxe_profile_id`` and side-steps
    # the collection-joined-load constraint. Bundle assembly does its
    # own targeted query in ``_assemble_pxe_classes`` anyway.
    pxe_profile: Mapped[DHCPPXEProfile | None] = relationship(
        "DHCPPXEProfile",
        foreign_keys=[pxe_profile_id],
        lazy="selectin",
    )


class DHCPPool(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A range within a scope: dynamic, excluded, or reserved."""

    __tablename__ = "dhcp_pool"
    __table_args__ = (Index("ix_dhcp_pool_scope", "scope_id"),)

    scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_scope.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    start_ip: Mapped[str] = mapped_column(INET, nullable=False)
    end_ip: Mapped[str] = mapped_column(INET, nullable=False)
    # pool_type: dynamic | excluded | reserved
    pool_type: Mapped[str] = mapped_column(String(20), nullable=False, default="dynamic")
    class_restriction: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_time_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    options_override: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    scope: Mapped[DHCPScope] = relationship("DHCPScope", back_populates="pools")


class DHCPStaticAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A DHCP reservation (MAC → IP) within a scope."""

    __tablename__ = "dhcp_static_assignment"
    __table_args__ = (
        UniqueConstraint("scope_id", "mac_address", name="uq_dhcp_static_scope_mac"),
        UniqueConstraint("scope_id", "ip_address", name="uq_dhcp_static_scope_ip"),
        Index("ix_dhcp_static_scope", "scope_id"),
        Index("ix_dhcp_static_mac", "mac_address"),
    )

    scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_scope.id", ondelete="CASCADE"),
        nullable=False,
    )
    ip_address: Mapped[str] = mapped_column(INET, nullable=False)
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    options_override: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Free-form ``key → value`` labels (issue #104).
    tags: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )

    scope: Mapped[DHCPScope] = relationship("DHCPScope", back_populates="statics")


class DHCPClientClass(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named client class for conditional option delivery — group-wide."""

    __tablename__ = "dhcp_client_class"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_dhcp_client_class_group_name"),
        Index("ix_dhcp_client_class_group", "group_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    match_expression: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {})

    group: Mapped[DHCPServerGroup] = relationship(
        "DHCPServerGroup", back_populates="client_classes"
    )


class DHCPOptionTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named bundle of DHCP option-code → value pairs — group-scoped.

    Operators apply a template to a scope (or pool/static in a future
    iteration) to stamp multiple options at once instead of editing them
    individually. The ``options`` JSONB shape mirrors ``DHCPScope.options``
    (``{name: value}``) so apply == merge-by-key.

    Templates are advisory bundles only — they do not flow into the
    ConfigBundle directly. Applying a template copies its options into
    the target scope's options dict at the moment of apply; subsequent
    template edits do NOT propagate back to scopes that already used it.
    """

    __tablename__ = "dhcp_option_template"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_dhcp_option_template_group_name"),
        Index("ix_dhcp_option_template_group", "group_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    address_family: Mapped[str] = mapped_column(
        String(4), nullable=False, default="ipv4", server_default="ipv4"
    )
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {})

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    group: Mapped[DHCPServerGroup] = relationship(
        "DHCPServerGroup", back_populates="option_templates"
    )


# ── MAC blocklist ───────────────────────────────────────────────────────────


class DHCPMACBlock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A blocked MAC address — group-global, applies to every scope.

    Under Kea this becomes part of the reserved ``DROP`` client class's
    ``test`` expression, rendered by the agent. Under Windows DHCP the
    agentless driver pushes an ``Add-DhcpServerv4Filter -List Deny`` row
    on every member server via WinRM. Expired rows are filtered out of
    the rendered config on every ``ConfigBundle`` build; a beat tick
    notices the state transition and forces a re-push so the operator
    doesn't have to.
    """

    __tablename__ = "dhcp_mac_block"
    __table_args__ = (
        UniqueConstraint("group_id", "mac_address", name="uq_dhcp_mac_block_group_mac"),
        Index("ix_dhcp_mac_block_group", "group_id"),
        Index("ix_dhcp_mac_block_mac", "mac_address"),
        Index("ix_dhcp_mac_block_expires_at", "expires_at"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=False)
    # reason: rogue | lost_stolen | quarantine | policy | other
    reason: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Populated by agents reporting a drop — optional telemetry.
    last_match_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    group: Mapped[DHCPServerGroup] = relationship("DHCPServerGroup", back_populates="mac_blocks")


# ── Leases ──────────────────────────────────────────────────────────────────


class DHCPLease(UUIDPrimaryKeyMixin, Base):
    """An active or historical DHCP lease reported by an agent.

    Per-server because each Kea instance owns its own memfile. Under HA
    the partner syncs leases via ``libdhcp_ha.so``, but the memfile is
    still local — so one lease event arrives here twice (once from each
    peer). The scope_id link points to the group-level scope the lease
    matches.
    """

    __tablename__ = "dhcp_lease"
    __table_args__ = (
        Index("ix_dhcp_lease_server_ip", "server_id", "ip_address"),
        Index("ix_dhcp_lease_server_mac", "server_id", "mac_address"),
        Index("ix_dhcp_lease_state", "state"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_scope.id", ondelete="SET NULL"),
        nullable=True,
    )
    ip_address: Mapped[str] = mapped_column(INET, nullable=False)
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_class: Mapped[str | None] = mapped_column(String(255), nullable=True)

    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    server: Mapped[DHCPServer] = relationship("DHCPServer", back_populates="leases")


# ── Agent op queue ──────────────────────────────────────────────────────────


class DHCPConfigOp(UUIDPrimaryKeyMixin, Base):
    """Queued op for the agent to apply (config push, restart, reload)."""

    __tablename__ = "dhcp_config_op"
    __table_args__ = (Index("ix_dhcp_config_op_server_status", "server_id", "status"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    op_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {})
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


DHCPRecordOp = DHCPConfigOp


class DHCPLeaseHistory(UUIDPrimaryKeyMixin, Base):
    """Historical record of a DHCP lease that left the active set.

    Written on absence-delete (pull_leases), time-based expiry sweep
    (dhcp_lease_cleanup), and MAC-reassignment within pull_leases.
    Retained for PlatformSettings.dhcp_lease_history_retention_days days
    (default 90).
    """

    __tablename__ = "dhcp_lease_history"
    __table_args__ = (
        Index("ix_dhcp_lease_history_server_id", "server_id"),
        Index("ix_dhcp_lease_history_ip_address", "ip_address"),
        Index("ix_dhcp_lease_history_mac_address", "mac_address"),
        Index("ix_dhcp_lease_history_server_expired", "server_id", "expired_at"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_scope.id", ondelete="SET NULL"),
        nullable=True,
    )
    ip_address: Mapped[str] = mapped_column(INET, nullable=False)
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_state: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DHCPPXEProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reusable PXE / iPXE provisioning profile (issue #51).

    Group-scoped (mirrors how scopes / pools / statics live on
    ``DHCPServerGroup``). One profile carries N arch-matches; an
    operator picks one profile per scope via
    ``DHCPScope.pxe_profile_id``. Disabled profiles render no
    classes, letting an operator A/B-test boot files without
    deleting the configuration.

    ``next_server`` is the IPv4 of the TFTP / HTTP boot server. The
    matches each carry a vendor_class + arch-code filter and the
    boot file the matched client should download (ipxe.efi /
    undionly.kpxe / a chained iPXE config URL / etc).
    """

    __tablename__ = "dhcp_pxe_profile"
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_dhcp_pxe_profile_group_name"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_server: Mapped[str] = mapped_column(String(45), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    matches: Mapped[list[DHCPPXEArchMatch]] = relationship(
        "DHCPPXEArchMatch",
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="joined",
        order_by="DHCPPXEArchMatch.priority",
    )


class DHCPPXEArchMatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One arch-match row within a PXE profile.

    Each match describes a (vendor_class_substring, arch_code_set)
    filter and the boot filename (TFTP) or URL (HTTP / iPXE chain)
    the matched client should download.

    ``priority`` is the deterministic tie-breaker — Kea evaluates
    client-classes in declared order, so the renderer emits matches
    in (priority ASC, id ASC) so config diffs stay stable across
    runs and most-specific matches fire first when the operator
    orders them right.

    ``match_kind`` is informational + drives the UI's preset boot-
    filename hints; the renderer treats both kinds the same. Kea
    sees a class either way.
    """

    __tablename__ = "dhcp_pxe_arch_match"
    __table_args__ = (Index("ix_dhcp_pxe_arch_match_profile_priority", "profile_id", "priority"),)

    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_pxe_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # match_kind: first_stage | ipxe_chain
    match_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="first_stage")
    # Substring match on DHCP option 60 (vendor class identifier).
    # ``PXEClient`` for first-stage TFTP boot, ``iPXE`` for the
    # chained iPXE GET, ``HTTPClient`` for UEFI HTTP boot. Null =
    # match anything (paired with arch_codes to filter).
    vendor_class_match: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # List of DHCP option 93 (Client Architecture Type) values to
    # match — see issue #51 for the canonical lookup table. Null =
    # match any arch (paired with vendor_class_match to filter).
    arch_codes: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    boot_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    boot_file_url_v6: Mapped[str | None] = mapped_column(String(512), nullable=True)

    profile: Mapped[DHCPPXEProfile] = relationship("DHCPPXEProfile", back_populates="matches")


class DHCPPhoneProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reusable VoIP phone provisioning profile (issue #112 phase 1).

    Group-scoped (mirrors how scopes / pools / statics / PXE profiles
    live on ``DHCPServerGroup``). One profile carries:

    - a ``vendor_class_match`` substring (option-60 vendor-class-id)
      that fences which clients receive its option set
    - an ``option_set`` JSONB list of ``{code, name, value}`` triples
      delivered as Kea ``option-data`` when the match fires

    Attached to one or more scopes via the ``dhcp_phone_profile_scope``
    join table — the same profile can be reused across multiple voice
    VLANs without copy-pasting the option set. The Kea driver emits
    one client-class per profile (gated by the vendor-class match);
    Kea evaluates classes globally, so a profile attached to *any*
    scope drives lease-time options for matching clients group-wide.
    """

    __tablename__ = "dhcp_phone_profile"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_dhcp_phone_profile_group_name"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Curated vendor label from the VoIP options catalog (Polycom /
    # Yealink / Cisco SPA / etc). Optional — operators can roll their
    # own profile that doesn't map to a curated vendor.
    vendor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Substring match on DHCP option-60 (vendor-class-id). Empty / null
    # means "always match" (paired with a low priority + scope
    # attachment for fencing).
    vendor_class_match: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Option set delivered when the match fires. Shape:
    # ``[{"code": 66, "name": "tftp-server-name", "value": "..."}, ...]``
    # ``name`` is the Kea option-data name (or the SpatiumDDI alias);
    # the renderer prefers the curated name from the option-code library
    # when ``code`` is set and ``name`` is omitted.
    option_set: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class DHCPPhoneProfileScope(Base):
    """M:N join — a phone profile can attach to many scopes and vice versa."""

    __tablename__ = "dhcp_phone_profile_scope"

    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_phone_profile.id", ondelete="CASCADE"),
        primary_key=True,
    )
    scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_scope.id", ondelete="CASCADE"),
        primary_key=True,
    )


__all__ = [
    "DHCPServerGroup",
    "DHCPServer",
    "DHCPScope",
    "DHCPPool",
    "DHCPStaticAssignment",
    "DHCPClientClass",
    "DHCPOptionTemplate",
    "DHCPMACBlock",
    "DHCPPXEProfile",
    "DHCPPXEArchMatch",
    "DHCPPhoneProfile",
    "DHCPPhoneProfileScope",
    "DHCPLease",
    "DHCPConfigOp",
    "DHCPRecordOp",
    "DHCPLeaseHistory",
]
