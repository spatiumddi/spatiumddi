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
from sqlalchemy.dialects.postgresql import INET, JSONB, MACADDR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

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
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    agent_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_pushed_at: Mapped[datetime | None] = mapped_column(
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


class DHCPScope(UUIDPrimaryKeyMixin, TimestampMixin, Base):
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


__all__ = [
    "DHCPServerGroup",
    "DHCPServer",
    "DHCPScope",
    "DHCPPool",
    "DHCPStaticAssignment",
    "DHCPClientClass",
    "DHCPLease",
    "DHCPConfigOp",
    "DHCPRecordOp",
]
