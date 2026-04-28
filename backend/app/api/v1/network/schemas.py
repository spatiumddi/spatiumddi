"""Pydantic schemas for the Network Discovery API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class NetworkDeviceCreate(BaseModel):
    """Operator-supplied fields for registering a new SNMP-polled device.

    Either a v1/v2c community OR a v3 security_name must be supplied
    — enforced by the model validator below. Plaintext secrets land in
    the encrypted columns server-side; the response shape never echoes
    them back.
    """

    # Identity
    name: str
    ip_address: str  # bare IP literal (not a CIDR) — used for SNMP transport
    hostname: str = ""  # optional FQDN, display only
    device_type: Literal["router", "switch", "ap", "firewall", "l3_switch", "other"] = "other"
    description: str | None = None

    # Transport
    snmp_version: Literal["v1", "v2c", "v3"] = "v2c"
    snmp_port: int = 161
    snmp_timeout_seconds: int = 5
    snmp_retries: int = 2

    # Auth (one branch required — see _check_auth_branch)
    community: str | None = None  # v1 / v2c
    v3_security_name: str | None = None
    v3_security_level: Literal["noAuthNoPriv", "authNoPriv", "authPriv"] | None = None
    v3_auth_protocol: Literal["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"] | None = None
    v3_auth_key: str | None = None
    v3_priv_protocol: Literal["DES", "3DES", "AES128", "AES192", "AES256"] | None = None
    v3_priv_key: str | None = None
    v3_context_name: str | None = None

    # Polling
    poll_interval_seconds: int = 300
    poll_arp: bool = True
    poll_fdb: bool = True
    poll_interfaces: bool = True
    poll_lldp: bool = True
    auto_create_discovered: bool = False

    # Binding
    ip_space_id: uuid.UUID
    is_active: bool = True
    tags: dict = Field(default_factory=dict)

    @field_validator("snmp_port")
    @classmethod
    def _valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("snmp_port must be 1-65535")
        return v

    @field_validator("snmp_timeout_seconds", "snmp_retries", "poll_interval_seconds")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be ≥ 0")
        return v

    @model_validator(mode="after")
    def _check_auth_branch(self) -> NetworkDeviceCreate:
        if self.snmp_version in ("v1", "v2c"):
            if not self.community:
                raise ValueError("community is required for v1 / v2c")
        elif self.snmp_version == "v3":
            if not self.v3_security_name:
                raise ValueError("v3_security_name is required for v3")
            level = self.v3_security_level or "noAuthNoPriv"
            if level in ("authNoPriv", "authPriv"):
                if not self.v3_auth_protocol or not self.v3_auth_key:
                    raise ValueError(
                        "v3_auth_protocol + v3_auth_key required for authNoPriv / authPriv"
                    )
            if level == "authPriv":
                if not self.v3_priv_protocol or not self.v3_priv_key:
                    raise ValueError("v3_priv_protocol + v3_priv_key required for authPriv")
        return self


class NetworkDeviceUpdate(BaseModel):
    """Partial update — every field is Optional. Re-uploading a secret
    rotates it; omitting / blanking keeps the stored value."""

    name: str | None = None
    hostname: str | None = None
    ip_address: str | None = None
    device_type: Literal["router", "switch", "ap", "firewall", "l3_switch", "other"] | None = None
    description: str | None = None

    snmp_version: Literal["v1", "v2c", "v3"] | None = None
    snmp_port: int | None = None
    snmp_timeout_seconds: int | None = None
    snmp_retries: int | None = None

    community: str | None = None
    v3_security_name: str | None = None
    v3_security_level: Literal["noAuthNoPriv", "authNoPriv", "authPriv"] | None = None
    v3_auth_protocol: Literal["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"] | None = None
    v3_auth_key: str | None = None
    v3_priv_protocol: Literal["DES", "3DES", "AES128", "AES192", "AES256"] | None = None
    v3_priv_key: str | None = None
    v3_context_name: str | None = None

    poll_interval_seconds: int | None = None
    poll_arp: bool | None = None
    poll_fdb: bool | None = None
    poll_interfaces: bool | None = None
    poll_lldp: bool | None = None
    auto_create_discovered: bool | None = None

    ip_space_id: uuid.UUID | None = None
    is_active: bool | None = None
    tags: dict | None = None


class NetworkDeviceRead(BaseModel):
    """Operator-facing view — never includes secrets, just presence flags."""

    id: uuid.UUID
    name: str
    ip_address: str
    hostname: str
    device_type: str
    vendor: str | None
    sys_descr: str | None
    sys_object_id: str | None
    sys_name: str | None
    sys_uptime_seconds: int | None
    description: str | None

    snmp_version: str
    snmp_port: int
    snmp_timeout_seconds: int
    snmp_retries: int
    has_community: bool
    v3_security_name: str | None
    v3_security_level: str | None
    v3_auth_protocol: str | None
    has_auth_key: bool
    v3_priv_protocol: str | None
    has_priv_key: bool
    v3_context_name: str | None

    poll_interval_seconds: int
    poll_arp: bool
    poll_fdb: bool
    poll_interfaces: bool
    poll_lldp: bool
    auto_create_discovered: bool

    last_poll_at: datetime | None
    next_poll_at: datetime | None
    last_poll_status: str
    last_poll_error: str | None
    last_poll_arp_count: int | None
    last_poll_fdb_count: int | None
    last_poll_interface_count: int | None
    last_poll_neighbour_count: int | None

    ip_space_id: uuid.UUID
    ip_space_name: str | None
    is_active: bool
    tags: dict

    created_at: datetime
    modified_at: datetime


class TestConnectionResult(BaseModel):
    success: bool
    sys_descr: str | None = None
    sys_object_id: str | None = None
    sys_name: str | None = None
    vendor: str | None = None
    error_kind: (
        Literal["timeout", "auth_failure", "no_response", "transport_error", "internal"] | None
    ) = None
    error_message: str | None = None
    elapsed_ms: int


class PollNowResult(BaseModel):
    task_id: str
    queued_at: datetime


class NetworkInterfaceRead(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    if_index: int
    name: str
    alias: str | None
    description: str | None
    speed_bps: int | None
    mac_address: str | None
    admin_status: str | None
    oper_status: str | None
    last_change_seconds: int | None
    created_at: datetime
    modified_at: datetime

    @field_validator("mac_address", mode="before")
    @classmethod
    def _coerce_mac(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class NetworkArpRead(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    interface_id: uuid.UUID | None
    interface_name: str | None = None  # joined for UI convenience
    ip_address: str
    mac_address: str
    vrf_name: str | None
    address_type: str
    state: str
    first_seen: datetime
    last_seen: datetime

    @field_validator("ip_address", "mac_address", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class NetworkFdbRead(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    interface_id: uuid.UUID
    interface_name: str | None = None  # joined for UI convenience
    mac_address: str
    vlan_id: int | None
    fdb_type: str
    first_seen: datetime
    last_seen: datetime

    @field_validator("mac_address", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class NetworkContextEntry(BaseModel):
    """Per-FDB-hit context attached to an IP address by the
    ``/ipam/addresses/{address_id}/network-context`` endpoint.

    Multiple rows are possible — a hypervisor with multiple VMs sharing
    a MAC across VLANs surfaces one entry per (device × VLAN × port).
    """

    device_id: uuid.UUID
    device_name: str
    interface_id: uuid.UUID
    interface_name: str
    interface_alias: str | None
    vlan_id: int | None
    mac_address: str
    fdb_type: str
    last_seen: datetime

    @field_validator("mac_address", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        return str(v) if v is not None else v


# ── Paginated list wrappers ────────────────────────────────────────────
# The frontend list pages were built against a {items, total, page,
# page_size} shape. The wrappers below let every list endpoint return
# the same envelope so React Query can render counts + pagination
# controls without per-endpoint adapters.


class NetworkDeviceListResponse(BaseModel):
    items: list[NetworkDeviceRead]
    total: int
    page: int
    page_size: int


class NetworkInterfaceListResponse(BaseModel):
    items: list[NetworkInterfaceRead]
    total: int
    page: int
    page_size: int


class NetworkArpListResponse(BaseModel):
    items: list[NetworkArpRead]
    total: int
    page: int
    page_size: int


class NetworkFdbListResponse(BaseModel):
    items: list[NetworkFdbRead]
    total: int
    page: int
    page_size: int


class NetworkNeighbourRead(BaseModel):
    """One LLDP neighbour seen on a local interface.

    The ``*_subtype`` integers come straight from the wire so the
    frontend can render the matching label (mac vs interfaceName vs
    local etc). The frontend's ``LLDP_CHASSIS_ID_SUBTYPES`` /
    ``LLDP_PORT_ID_SUBTYPES`` maps are kept in sync with the backend
    poller's enums.
    """

    id: uuid.UUID
    device_id: uuid.UUID
    interface_id: uuid.UUID | None
    interface_name: str | None = None  # joined for UI convenience
    local_port_num: int
    remote_chassis_id_subtype: int
    remote_chassis_id: str
    remote_port_id_subtype: int
    remote_port_id: str
    remote_port_desc: str | None
    remote_sys_name: str | None
    remote_sys_desc: str | None
    remote_sys_cap_enabled: int | None
    first_seen: datetime
    last_seen: datetime


class NetworkNeighbourListResponse(BaseModel):
    items: list[NetworkNeighbourRead]
    total: int
    page: int
    page_size: int


__all__ = [
    "NetworkDeviceCreate",
    "NetworkDeviceUpdate",
    "NetworkDeviceRead",
    "NetworkDeviceListResponse",
    "TestConnectionResult",
    "PollNowResult",
    "NetworkInterfaceRead",
    "NetworkInterfaceListResponse",
    "NetworkArpRead",
    "NetworkArpListResponse",
    "NetworkFdbRead",
    "NetworkFdbListResponse",
    "NetworkContextEntry",
]
