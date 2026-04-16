"""DHCP data models: server groups, servers, scopes, pools, static assignments,
client classes, leases, and agent op queue.

Mirrors the DNS module (see app.models.dns) for agent bookkeeping so the
DHCP agent runtime can reuse the same long-poll + ETag + op-ack patterns.
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
    """Logical cluster of DHCP servers (HA pair / failover partners)."""

    __tablename__ = "dhcp_server_group"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # mode: load-balancing | hot-standby
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="hot-standby")

    servers: Mapped[list[DHCPServer]] = relationship(
        "DHCPServer", back_populates="group", cascade="all, delete-orphan"
    )


class DHCPServer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Individual DHCP server managed by SpatiumDDI."""

    __tablename__ = "dhcp_server"
    __table_args__ = (
        UniqueConstraint("name", name="uq_dhcp_server_name"),
        Index("ix_dhcp_server_agent_id", "agent_id", unique=True),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # driver: kea | isc_dhcp
    driver: Mapped[str] = mapped_column(String(50), nullable=False, default="kea")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=67)
    # roles: primary | secondary | standalone (JSON array of strings)
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

    group: Mapped[DHCPServerGroup | None] = relationship(
        "DHCPServerGroup", back_populates="servers", lazy="joined"
    )
    scopes: Mapped[list[DHCPScope]] = relationship(
        "DHCPScope", back_populates="server", cascade="all, delete-orphan"
    )
    client_classes: Mapped[list[DHCPClientClass]] = relationship(
        "DHCPClientClass", back_populates="server", cascade="all, delete-orphan"
    )
    leases: Mapped[list[DHCPLease]] = relationship(
        "DHCPLease", back_populates="server", cascade="all, delete-orphan"
    )


# ── Scope / Pool / Static / Client Class ─────────────────────────────────────


class DHCPScope(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A DHCP scope — one subnet served by one DHCP server.

    Multiple servers can serve the same subnet (HA pair) via distinct scope rows.
    """

    __tablename__ = "dhcp_scope"
    __table_args__ = (
        UniqueConstraint("server_id", "subnet_id", name="uq_dhcp_scope_server_subnet"),
        Index("ix_dhcp_scope_server", "server_id"),
        Index("ix_dhcp_scope_subnet", "subnet_id"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    subnet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subnet.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    lease_time: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    min_lease_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_lease_time: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # DHCP options (JSONB map keyed by option name: routers, dns-servers,
    # domain-name, ntp-servers, tftp-server-name, bootfile-name,
    # tftp-server-address (150), etc.)
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {})

    # DDNS
    ddns_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ddns_hostname_policy: client | server_name | derived | none
    ddns_hostname_policy: Mapped[str] = mapped_column(String(30), nullable=False, default="client")
    # hostname_to_ipam_sync: disabled | on_lease | on_static_only
    hostname_to_ipam_sync: Mapped[str] = mapped_column(
        String(30), nullable=False, default="on_static_only"
    )

    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    server: Mapped[DHCPServer] = relationship("DHCPServer", back_populates="scopes")
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

    # Optional backlink into IPAM (if the IP is tracked there)
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
    """A named client class for conditional option delivery."""

    __tablename__ = "dhcp_client_class"
    __table_args__ = (
        UniqueConstraint("server_id", "name", name="uq_dhcp_client_class_server_name"),
        Index("ix_dhcp_client_class_server", "server_id"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    match_expression: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {})

    server: Mapped[DHCPServer] = relationship("DHCPServer", back_populates="client_classes")


# ── Leases ──────────────────────────────────────────────────────────────────


class DHCPLease(UUIDPrimaryKeyMixin, Base):
    """An active or historical DHCP lease reported by an agent."""

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

    # state: active | expired | released | abandoned
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    server: Mapped[DHCPServer] = relationship("DHCPServer", back_populates="leases")


# ── Agent op queue ──────────────────────────────────────────────────────────


class DHCPConfigOp(UUIDPrimaryKeyMixin, Base):
    """Queued op for the agent to apply (config push, restart, reload).

    Mirrors ``DNSRecordOp`` but broader: DHCP changes are usually whole-config
    reconfigurations (``apply_config``) rather than per-record deltas.
    """

    __tablename__ = "dhcp_config_op"
    __table_args__ = (Index("ix_dhcp_config_op_server_status", "server_id", "status"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server.id", ondelete="CASCADE"),
        nullable=False,
    )
    # op_type: apply_config | restart | reload
    op_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {})
    # status: pending | acked | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Alias: some callers use DHCPRecordOp terminology (mirroring DNSRecordOp).
# Keep a single underlying table to avoid schema drift.
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
