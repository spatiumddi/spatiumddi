"""Network discovery models — SNMP-polled routers / switches and the
ARP / FDB / interface tables they expose.

Vendor-neutral standard-MIB walk only:
  * SNMPv2-MIB system  → device identity (sysDescr / sysObjectID / …)
  * IF-MIB ifTable + ifXTable → ``network_interface``
  * IP-MIB ipNetToPhysicalTable (with RFC1213-MIB ipNetToMediaTable
    fallback) → ``network_arp_entry``
  * Q-BRIDGE-MIB dot1qTpFdbTable (with BRIDGE-MIB dot1dTpFdbTable
    fallback) → ``network_fdb_entry``

ARP cross-reference back into IPAM is handled in
``app.services.snmp.cross_reference``: every successful poll updates
``IPAddress.last_seen_at`` / ``last_seen_method='snmp'`` and (when the
device opts in) inserts ``status='discovered'`` rows for ARP IPs that
fall inside an existing ``Subnet`` in the device's bound ``ip_space_id``.

Stale ARP entries that disappear from the wire stay around with
``state='stale'``; a janitor task purges them after 30 days. FDB rows
are absence-deleted on every poll.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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


class NetworkDevice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A managed network device polled over SNMP.

    Bound to exactly one ``IPSpace`` — ARP entries discovered here are
    cross-referenced into IPAM rows that live in the same space. Many
    devices may share a space (e.g. every switch in a campus VLAN).

    Credentials are Fernet-encrypted at rest. v1 / v2c uses
    ``community_encrypted``; v3 USM uses
    ``v3_security_name`` + the protocol/key columns. Plaintext secrets
    are never returned by the API — list / get responses expose
    ``has_community`` / ``has_auth_key`` / ``has_priv_key`` booleans
    instead.
    """

    __tablename__ = "network_device"
    __table_args__ = (
        UniqueConstraint("name", name="uq_network_device_name"),
        Index("ix_network_device_name", "name"),
        Index("ix_network_device_next_poll_at", "next_poll_at"),
    )

    # ── Identity ────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str] = mapped_column(INET, nullable=False)
    # device_type: router | switch | ap | firewall | l3_switch | other
    device_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="other", server_default="other"
    )
    vendor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sys_descr: Mapped[str | None] = mapped_column(Text, nullable=True)
    sys_object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sys_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sys_uptime_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # ── SNMP transport ─────────────────────────────────────────────────
    # snmp_version: v1 | v2c | v3
    snmp_version: Mapped[str] = mapped_column(
        String(8), nullable=False, default="v2c", server_default="v2c"
    )
    snmp_port: Mapped[int] = mapped_column(
        Integer, nullable=False, default=161, server_default="161"
    )
    snmp_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    snmp_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    community_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # ── SNMPv3 USM ─────────────────────────────────────────────────────
    v3_security_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # v3_security_level: noAuthNoPriv | authNoPriv | authPriv
    v3_security_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # v3_auth_protocol: MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512
    v3_auth_protocol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    v3_auth_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # v3_priv_protocol: DES | 3DES | AES128 | AES192 | AES256
    v3_priv_protocol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    v3_priv_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Reserved for future VRF / SNMPv3 context support. The poller does
    # not loop over VRFs in v1 — column exists so we can populate it
    # without a follow-up migration.
    v3_context_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Polling cadence + scope ────────────────────────────────────────
    poll_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )
    poll_arp: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    poll_fdb: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    poll_interfaces: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    auto_create_discovered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # last_poll_status: pending | success | partial | failed | timeout
    last_poll_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    last_poll_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_poll_arp_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_poll_fdb_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_poll_interface_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Binding ────────────────────────────────────────────────────────
    ip_space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_space.id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    # ── Relationships ──────────────────────────────────────────────────
    interfaces: Mapped[list[NetworkInterface]] = relationship(
        "NetworkInterface",
        back_populates="device",
        cascade="all, delete-orphan",
    )
    arp_entries: Mapped[list[NetworkArpEntry]] = relationship(
        "NetworkArpEntry",
        back_populates="device",
        cascade="all, delete-orphan",
    )
    fdb_entries: Mapped[list[NetworkFdbEntry]] = relationship(
        "NetworkFdbEntry",
        back_populates="device",
        cascade="all, delete-orphan",
    )
    neighbours: Mapped[list[NetworkNeighbour]] = relationship(
        "NetworkNeighbour",
        back_populates="device",
        cascade="all, delete-orphan",
    )

    last_poll_neighbour_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poll_lldp: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class NetworkInterface(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One physical / logical interface on a polled device.

    Pulled from IF-MIB ifTable + ifXTable. ``if_index`` is the SNMP
    index used as the join key for ARP and FDB entries; not stable
    across reboots on most platforms but stable for the duration of a
    single poll, which is all we need.
    """

    __tablename__ = "network_interface"
    __table_args__ = (
        UniqueConstraint("device_id", "if_index", name="uq_network_interface_device_ifindex"),
        Index("ix_network_interface_device", "device_id"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_device.id", ondelete="CASCADE"),
        nullable=False,
    )
    if_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    speed_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mac_address: Mapped[str | None] = mapped_column(MACADDR, nullable=True)
    # admin_status: up | down | testing
    admin_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # oper_status: up | down | testing | unknown | dormant | notPresent | lowerLayerDown
    oper_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_change_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    device: Mapped[NetworkDevice] = relationship("NetworkDevice", back_populates="interfaces")


class NetworkArpEntry(UUIDPrimaryKeyMixin, Base):
    """An ARP / IPv6-ND entry discovered via IP-MIB ipNetToPhysicalTable.

    One row per (device, ip, vrf) tuple. ``state='stale'`` rows are
    kept on the wire for forensic purposes; a janitor task purges
    rows whose ``last_seen`` is older than 30 days.
    """

    __tablename__ = "network_arp_entry"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "ip_address", "vrf_name", name="uq_network_arp_device_ip_vrf"
        ),
        Index("ix_network_arp_device", "device_id"),
        Index("ix_network_arp_mac", "mac_address"),
        Index("ix_network_arp_ip", "ip_address"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_device.id", ondelete="CASCADE"),
        nullable=False,
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_interface.id", ondelete="SET NULL"),
        nullable=True,
    )
    ip_address: Mapped[str] = mapped_column(INET, nullable=False)
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=False)
    vrf_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # address_type: ipv4 | ipv6
    address_type: Mapped[str] = mapped_column(String(8), nullable=False)
    # state: reachable | stale | delay | probe | invalid | unknown
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default="unknown"
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    device: Mapped[NetworkDevice] = relationship("NetworkDevice", back_populates="arp_entries")


class NetworkFdbEntry(UUIDPrimaryKeyMixin, Base):
    """A bridge forwarding-database entry discovered via Q-BRIDGE-MIB.

    One row per ``(device, mac, vlan)`` tuple — modern switches keep
    distinct entries per VLAN even for the same MAC. ``vlan_id`` is
    NULL when the device only exposes the legacy BRIDGE-MIB
    ``dot1dTpFdbTable`` (which is VLAN-unaware); the unique index uses
    Postgres 15+ ``NULLS NOT DISTINCT`` so absent VLAN tags still
    de-duplicate properly.
    """

    __tablename__ = "network_fdb_entry"
    __table_args__ = (
        Index(
            "ix_network_fdb_device_mac_vlan_unique",
            "device_id",
            "mac_address",
            "vlan_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_network_fdb_device", "device_id"),
        Index("ix_network_fdb_mac", "mac_address"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_device.id", ondelete="CASCADE"),
        nullable=False,
    )
    interface_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_interface.id", ondelete="CASCADE"),
        nullable=False,
    )
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=False)
    vlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # fdb_type: learned | static | mgmt | other
    fdb_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="learned", server_default="learned"
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    device: Mapped[NetworkDevice] = relationship("NetworkDevice", back_populates="fdb_entries")


class NetworkNeighbour(UUIDPrimaryKeyMixin, Base):
    """An LLDP neighbour discovered on a local interface.

    One row per ``(device, local_interface, remote_chassis_id,
    remote_port_id)`` tuple — that's the same compound key LLDP
    itself uses to dedupe neighbours over time. ``chassis_id`` and
    ``port_id`` are stored verbatim as decoded by the poller (MAC
    string for the common subtypes, hex for vendor-defined ones);
    the matching ``*_subtype`` columns let the UI render correctly.

    FDB rows are absence-deleted on every poll; we mirror that here
    — neighbours that aged out of the wire's lldpRemTable disappear
    from the DB on the next poll. Operators looking for historical
    "what used to be plugged here" should use the audit log.
    """

    __tablename__ = "network_neighbour"
    __table_args__ = (
        UniqueConstraint(
            "device_id",
            "interface_id",
            "remote_chassis_id",
            "remote_port_id",
            name="uq_network_neighbour_device_iface_remote",
        ),
        Index("ix_network_neighbour_device", "device_id"),
        Index("ix_network_neighbour_remote_sys_name", "remote_sys_name"),
        Index("ix_network_neighbour_remote_chassis_id", "remote_chassis_id"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_device.id", ondelete="CASCADE"),
        nullable=False,
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_interface.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Raw local-port-num from the LLDP index. We persist it
    # alongside ``interface_id`` because devices that haven't been
    # polled for ifTable yet still need a stable identifier here.
    local_port_num: Mapped[int] = mapped_column(Integer, nullable=False)

    # 1=chassisComponent, 2=interfaceAlias, 3=portComponent,
    # 4=macAddress, 5=networkAddress, 6=interfaceName, 7=local —
    # see LLDP-MIB LldpChassisIdSubtype.
    remote_chassis_id_subtype: Mapped[int] = mapped_column(Integer, nullable=False)
    remote_chassis_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 1=interfaceAlias, 2=portComponent, 3=macAddress, 4=networkAddress,
    # 5=interfaceName, 6=agentCircuitId, 7=local — LldpPortIdSubtype.
    remote_port_id_subtype: Mapped[int] = mapped_column(Integer, nullable=False)
    remote_port_id: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_port_desc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_sys_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_sys_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bitmask per LldpSystemCapabilitiesMap (1=other, 2=repeater,
    # 4=bridge, 8=wlanAccessPoint, 16=router, 32=telephone,
    # 64=docsisCableDevice, 128=stationOnly, 256=cVLANComponent,
    # 512=sVLANComponent, 1024=twoPortMACRelay).
    remote_sys_cap_enabled: Mapped[int | None] = mapped_column(Integer, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    device: Mapped[NetworkDevice] = relationship("NetworkDevice", back_populates="neighbours")


__all__ = [
    "NetworkDevice",
    "NetworkInterface",
    "NetworkArpEntry",
    "NetworkFdbEntry",
    "NetworkNeighbour",
]
