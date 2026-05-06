"""DNS API: server groups, servers, server options, views, ACLs, zones, records."""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.core.crypto import decrypt_dict, encrypt_dict, encrypt_str
from app.core.permissions import require_any_resource_permission
from app.drivers.dns import _DRIVERS as _DNS_DRIVERS
from app.drivers.dns import is_agentless
from app.drivers.dns.windows import test_winrm_credentials
from app.models.audit import AuditLog
from app.models.dns import (
    DNSAcl,
    DNSAclEntry,
    DNSRecord,
    DNSServer,
    DNSServerGroup,
    DNSServerOptions,
    DNSServerRuntimeState,
    DNSTrustAnchor,
    DNSTSIGKey,
    DNSView,
    DNSZone,
)
from app.services.dns.delegation import (
    compute_delegation,
    find_parent_zone,
    preview_to_dict,
)
from app.services.dns.record_ops import enqueue_record_op, enqueue_record_ops_batch
from app.services.dns.serial import bump_zone_serial
from app.services.dns.zone_templates import (
    get_template,
    list_templates,
    materialize,
    validate_params,
)
from app.services.dns_io import (
    RecordChange,
    ZoneParseError,
    diff_records,
    parse_zone_file,
    write_zone_file,
)
from app.services.soft_delete import (
    apply_soft_delete,
    collect_soft_delete_batch,
)
from app.services.tags import apply_tag_filter

logger = structlog.get_logger(__name__)

# Router-level RBAC: GET=read, POST/PUT/PATCH=write, DELETE=delete. The DNS
# router covers server groups, servers, views, ACLs, zones and records, so a
# user with `admin` on any of (dns_group, dns_zone, dns_record) passes the
# router gate. Handlers that need finer scoping (e.g. "record write permitted
# only when dns_zone permits write") can do inline checks with
# `user_has_permission`.
router = APIRouter(
    dependencies=[Depends(require_any_resource_permission("dns_group", "dns_zone", "dns_record"))]
)

# Sourced from the driver registry so new drivers (e.g. ``windows_dns``)
# don't also need a schema edit to be accepted on create/update.
VALID_DRIVERS = frozenset(_DNS_DRIVERS.keys())
VALID_GROUP_TYPES = {"internal", "external", "dmz", "custom"}
VALID_ZONE_TYPES = {"primary", "secondary", "stub", "forward"}
VALID_RECORD_TYPES = {
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "NS",
    "PTR",
    "SRV",
    "CAA",
    "TLSA",
    "SSHFP",
    "NAPTR",
    "LOC",
}
VALID_FORWARD_POLICIES = {"first", "only"}
VALID_DNSSEC = {"auto", "yes", "no"}
VALID_NOTIFY = {"yes", "no", "explicit", "master-only"}


# ── Pydantic schemas ────────────────────────────────────────────────────────


class ServerGroupCreate(BaseModel):
    name: str
    description: str = ""
    group_type: str = "internal"
    default_view: str | None = None
    is_recursive: bool = True
    # BIND9 catalog zones (RFC 9432). Off by default — only meaningful in
    # ≥2-server BIND9 groups.
    catalog_zones_enabled: bool = False
    catalog_zone_name: str = "catalog.spatium.invalid."
    # Issue #25 — flag this group as exposed to the public internet.
    # The IPAM safety guard returns ``requires_confirmation`` when an
    # operator pins a private IP into a zone in this group, forcing a
    # typed-CIDR confirm. Off by default — existing groups unaffected.
    is_public_facing: bool = False

    @field_validator("group_type")
    @classmethod
    def validate_group_type(cls, v: str) -> str:
        if v not in VALID_GROUP_TYPES:
            raise ValueError(f"group_type must be one of {sorted(VALID_GROUP_TYPES)}")
        return v


class ServerGroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    group_type: str | None = None
    default_view: str | None = None
    is_recursive: bool | None = None
    catalog_zones_enabled: bool | None = None
    catalog_zone_name: str | None = None
    is_public_facing: bool | None = None

    @field_validator("group_type")
    @classmethod
    def validate_group_type(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_GROUP_TYPES:
            raise ValueError(f"group_type must be one of {sorted(VALID_GROUP_TYPES)}")
        return v


class ServerGroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    group_type: str
    default_view: str | None
    is_recursive: bool
    catalog_zones_enabled: bool
    catalog_zone_name: str
    is_public_facing: bool = False
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


# ── Server schemas ──────────────────────────────────────────────────────────


class WindowsCredentialsInput(BaseModel):
    """Windows DNS admin credentials (for driver='windows_dns' Path B).

    Stored Fernet-encrypted on ``DNSServer.credentials_encrypted``.
    Server never returns the password back — responses only expose
    ``has_credentials``.

    Mirrors the DHCP-side shape. All fields are optional to support
    **partial updates**: sending ``{"transport": "kerberos"}`` on an
    existing server decrypts the stored blob, merges the transport
    change, and re-encrypts. On first-time set, ``username`` + ``password``
    are still required — the endpoint validates that explicitly.
    """

    username: str | None = None
    password: str | None = None
    winrm_port: int | None = None
    # transport: ntlm | kerberos | basic | credssp
    transport: str | None = None
    use_tls: bool | None = None
    verify_tls: bool | None = None


class ServerCreate(BaseModel):
    name: str
    driver: str = "bind9"
    host: str
    port: int = 53
    api_port: int | None = None
    api_key: str | None = None
    roles: list[str] = []
    notes: str = ""
    is_enabled: bool = True
    # Only meaningful when driver='windows_dns' (Path B). Ignored
    # otherwise. Leaving this null on windows_dns is fine — the server
    # falls back to Path A (RFC 2136 record CRUD only, no zone topology
    # management).
    windows_credentials: WindowsCredentialsInput | None = None

    @field_validator("driver")
    @classmethod
    def validate_driver(cls, v: str) -> str:
        if v not in VALID_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(VALID_DRIVERS)}")
        return v


class ServerUpdate(BaseModel):
    name: str | None = None
    driver: str | None = None
    host: str | None = None
    port: int | None = None
    api_port: int | None = None
    api_key: str | None = None
    roles: list[str] | None = None
    status: str | None = None
    notes: str | None = None
    is_enabled: bool | None = None
    # Same contract as the DHCP side:
    #   * None → leave stored creds alone
    #   * {}   → clear stored creds (revert to Path A only)
    #   * dict → partial patch if server already has creds (decrypt-merge-
    #            reencrypt), full username+password if it doesn't
    windows_credentials: WindowsCredentialsInput | dict[str, Any] | None = None

    @field_validator("driver")
    @classmethod
    def validate_driver(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(VALID_DRIVERS)}")
        return v


class ServerResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    name: str
    driver: str
    host: str
    port: int
    api_port: int | None
    roles: list[str]
    status: str
    is_enabled: bool
    last_sync_at: datetime | None
    last_health_check_at: datetime | None
    notes: str
    # Surface capability flags so the UI can conditionally render the
    # Windows-specific affordances (credential form, Test Connection
    # button, Sync Zones from Server action) without hardcoding driver
    # names client-side.
    has_credentials: bool
    is_agentless: bool
    # Agent-state fields used by the Server Detail modal — surfacing
    # these lets operators answer "what's this server doing right now"
    # without crawling the database.
    agent_id: uuid.UUID | None
    last_seen_at: datetime | None
    last_config_etag: str | None
    pending_approval: bool
    is_primary: bool
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, s: DNSServer) -> ServerResponse:
        return cls(
            id=s.id,
            group_id=s.group_id,
            name=s.name,
            driver=s.driver,
            host=s.host,
            port=s.port,
            api_port=s.api_port,
            roles=list(s.roles or []),
            status=s.status,
            is_enabled=s.is_enabled,
            last_sync_at=s.last_sync_at,
            last_health_check_at=s.last_health_check_at,
            notes=s.notes,
            has_credentials=bool(s.credentials_encrypted),
            is_agentless=is_agentless(s.driver),
            agent_id=s.agent_id,
            last_seen_at=s.last_seen_at,
            last_config_etag=s.last_config_etag,
            pending_approval=s.pending_approval,
            is_primary=s.is_primary,
            created_at=s.created_at,
            modified_at=s.modified_at,
        )


class TestWindowsCredentialsRequest(BaseModel):
    """Pre-save dry-run: test a host + creds without writing them to the DB.

    For editing an existing server, omit ``credentials`` and pass the
    ``server_id`` — the endpoint decrypts the stored credentials and runs
    the same probe. If both are omitted, the request is rejected.
    """

    host: str
    credentials: WindowsCredentialsInput | None = None
    server_id: uuid.UUID | None = None


class TestResult(BaseModel):
    ok: bool
    message: str


class PullZonesResult(BaseModel):
    """Neutral shape for ``WindowsDNSDriver.pull_zones_from_server`` output."""

    zones: list[dict[str, Any]]


# ── Server options schemas ──────────────────────────────────────────────────


class TrustAnchorCreate(BaseModel):
    zone_name: str
    algorithm: int
    key_tag: int
    public_key: str
    is_initial_key: bool = True


class TrustAnchorResponse(BaseModel):
    id: uuid.UUID
    zone_name: str
    algorithm: int
    key_tag: int
    public_key: str
    is_initial_key: bool
    added_at: datetime

    model_config = {"from_attributes": True}


class ServerOptionsUpdate(BaseModel):
    forwarders: list[str] | None = None
    forward_policy: str | None = None
    recursion_enabled: bool | None = None
    allow_recursion: list[str] | None = None
    dnssec_validation: str | None = None
    gss_tsig_enabled: bool | None = None
    gss_tsig_keytab_path: str | None = None
    gss_tsig_realm: str | None = None
    gss_tsig_principal: str | None = None
    notify_enabled: str | None = None
    also_notify: list[str] | None = None
    allow_notify: list[str] | None = None
    allow_query: list[str] | None = None
    allow_query_cache: list[str] | None = None
    allow_transfer: list[str] | None = None
    blackhole: list[str] | None = None
    query_log_enabled: bool | None = None
    query_log_channel: str | None = None
    query_log_file: str | None = None
    query_log_severity: str | None = None
    query_log_print_category: bool | None = None
    query_log_print_severity: bool | None = None
    query_log_print_time: bool | None = None

    @field_validator("forward_policy")
    @classmethod
    def validate_forward_policy(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_FORWARD_POLICIES:
            raise ValueError(f"forward_policy must be one of {sorted(VALID_FORWARD_POLICIES)}")
        return v

    @field_validator("dnssec_validation")
    @classmethod
    def validate_dnssec(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_DNSSEC:
            raise ValueError(f"dnssec_validation must be one of {sorted(VALID_DNSSEC)}")
        return v

    @field_validator("notify_enabled")
    @classmethod
    def validate_notify(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_NOTIFY:
            raise ValueError(f"notify_enabled must be one of {sorted(VALID_NOTIFY)}")
        return v


class ServerOptionsResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    forwarders: list[str]
    forward_policy: str
    recursion_enabled: bool
    allow_recursion: list[str]
    dnssec_validation: str
    gss_tsig_enabled: bool
    gss_tsig_keytab_path: str | None
    gss_tsig_realm: str | None
    gss_tsig_principal: str | None
    notify_enabled: str
    also_notify: list[str]
    allow_notify: list[str]
    allow_query: list[str]
    allow_query_cache: list[str]
    allow_transfer: list[str]
    blackhole: list[str]
    query_log_enabled: bool
    query_log_channel: str
    query_log_file: str
    query_log_severity: str
    query_log_print_category: bool
    query_log_print_severity: bool
    query_log_print_time: bool
    trust_anchors: list[TrustAnchorResponse]
    modified_at: datetime

    model_config = {"from_attributes": True}


# ── ACL schemas ─────────────────────────────────────────────────────────────


class AclEntryCreate(BaseModel):
    value: str
    negate: bool = False
    order: int = 0


class AclEntryResponse(BaseModel):
    id: uuid.UUID
    value: str
    negate: bool
    order: int

    model_config = {"from_attributes": True}


class AclCreate(BaseModel):
    name: str
    description: str = ""
    entries: list[AclEntryCreate] = []


class AclUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    entries: list[AclEntryCreate] | None = None


class AclResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID | None
    name: str
    description: str
    entries: list[AclEntryResponse]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


# ── View schemas ────────────────────────────────────────────────────────────


class ViewCreate(BaseModel):
    name: str
    description: str = ""
    match_clients: list[str] = ["any"]
    match_destinations: list[str] = []
    recursion: bool = True
    order: int = 0
    # View-level query control overrides (fall back to server-group options if null)
    allow_query: list[str] | None = None
    allow_query_cache: list[str] | None = None


class ViewUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    match_clients: list[str] | None = None
    match_destinations: list[str] | None = None
    recursion: bool | None = None
    order: int | None = None
    allow_query: list[str] | None = None
    allow_query_cache: list[str] | None = None


class ViewResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    name: str
    description: str
    match_clients: list[str]
    match_destinations: list[str]
    recursion: bool
    order: int
    allow_query: list[str] | None = None
    allow_query_cache: list[str] | None = None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


# ── Zone schemas ────────────────────────────────────────────────────────────


VALID_ZONE_COLORS = {
    "slate",
    "red",
    "amber",
    "emerald",
    "cyan",
    "blue",
    "violet",
    "pink",
}


class ZoneCreate(BaseModel):
    name: str
    view_id: uuid.UUID | None = None
    zone_type: str = "primary"
    kind: str = "forward"
    ttl: int = 3600
    refresh: int = 86400
    retry: int = 7200
    expire: int = 3600000
    minimum: int = 3600
    primary_ns: str = ""
    admin_email: str = ""
    dnssec_enabled: bool = False
    color: str | None = None
    linked_subnet_id: uuid.UUID | None = None
    domain_id: uuid.UUID | None = None
    allow_query: list[str] | None = None
    allow_transfer: list[str] | None = None
    also_notify: list[str] | None = None
    notify_enabled: str | None = None
    # Forward-zone fields. Required when ``zone_type == "forward"``;
    # ignored otherwise.
    forwarders: list[str] = []
    forward_only: bool = True
    # Logical ownership (issue #91). Optional FK to the Customer that
    # owns this zone (managed-DNS engagements typically have one
    # customer per zone).
    customer_id: uuid.UUID | None = None
    tags: dict[str, Any] = Field(default_factory=dict)

    @field_validator("zone_type")
    @classmethod
    def validate_zone_type(cls, v: str) -> str:
        if v not in VALID_ZONE_TYPES:
            raise ValueError(f"zone_type must be one of {sorted(VALID_ZONE_TYPES)}")
        return v

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if v not in VALID_ZONE_COLORS:
            raise ValueError(f"color must be one of {sorted(VALID_ZONE_COLORS)}")
        return v

    @field_validator("name")
    @classmethod
    def ensure_trailing_dot(cls, v: str) -> str:
        return v if v.endswith(".") else v + "."


class ZoneUpdate(BaseModel):
    name: str | None = None
    view_id: uuid.UUID | None = None
    zone_type: str | None = None
    kind: str | None = None
    ttl: int | None = None
    refresh: int | None = None
    retry: int | None = None
    expire: int | None = None
    minimum: int | None = None
    primary_ns: str | None = None
    admin_email: str | None = None
    dnssec_enabled: bool | None = None
    color: str | None = None
    linked_subnet_id: uuid.UUID | None = None
    domain_id: uuid.UUID | None = None
    allow_query: list[str] | None = None
    allow_transfer: list[str] | None = None
    also_notify: list[str] | None = None
    notify_enabled: str | None = None
    forwarders: list[str] | None = None
    forward_only: bool | None = None
    customer_id: uuid.UUID | None = None
    tags: dict[str, Any] | None = None

    @field_validator("zone_type")
    @classmethod
    def validate_zone_type(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_ZONE_TYPES:
            raise ValueError(f"zone_type must be one of {sorted(VALID_ZONE_TYPES)}")
        return v

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        # "" clears the color; any other value must be in the curated set.
        if v is None or v == "":
            return None
        if v not in VALID_ZONE_COLORS:
            raise ValueError(f"color must be one of {sorted(VALID_ZONE_COLORS)}")
        return v

    @field_validator("notify_enabled")
    @classmethod
    def validate_notify(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_NOTIFY:
            raise ValueError(f"notify_enabled must be one of {sorted(VALID_NOTIFY)}")
        return v


class ZoneResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    view_id: uuid.UUID | None
    name: str
    zone_type: str
    kind: str
    ttl: int
    refresh: int
    retry: int
    expire: int
    minimum: int
    primary_ns: str
    admin_email: str
    is_auto_generated: bool
    linked_subnet_id: uuid.UUID | None
    domain_id: uuid.UUID | None = None
    dnssec_enabled: bool
    color: str | None
    last_serial: int
    last_pushed_at: datetime | None
    allow_query: list[str] | None
    allow_transfer: list[str] | None
    also_notify: list[str] | None
    notify_enabled: str | None
    forwarders: list[str]
    forward_only: bool
    # Non-null when the zone was synthesised by the Tailscale Phase 2
    # reconciler. The UI uses this to render a read-only badge and
    # disable edit/delete controls; the API enforces it on the
    # write paths regardless of UI state.
    tailscale_tenant_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


# ── Record schemas ──────────────────────────────────────────────────────────


class RecordCreate(BaseModel):
    name: str
    record_type: str
    value: str
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None
    view_id: uuid.UUID | None = None
    tags: dict[str, Any] = Field(default_factory=dict)

    @field_validator("record_type")
    @classmethod
    def validate_record_type(cls, v: str) -> str:
        v = v.upper()
        if v not in VALID_RECORD_TYPES:
            raise ValueError(f"record_type must be one of {sorted(VALID_RECORD_TYPES)}")
        return v


class RecordUpdate(BaseModel):
    name: str | None = None
    value: str | None = None
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None
    view_id: uuid.UUID | None = None
    tags: dict[str, Any] | None = None


class RecordResponse(BaseModel):
    id: uuid.UUID
    zone_id: uuid.UUID
    view_id: uuid.UUID | None
    name: str
    fqdn: str
    record_type: str
    value: str
    ttl: int | None
    priority: int | None
    weight: int | None
    port: int | None
    auto_generated: bool
    # Non-null when the record was synthesised by Tailscale Phase 2.
    tailscale_tenant_id: uuid.UUID | None = None
    # Non-null when the record is rendered by the DNS pool health-check
    # pipeline. Operator edits / deletes are blocked while non-null.
    pool_member_id: uuid.UUID | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


# ── Server Group endpoints ──────────────────────────────────────────────────


@router.get("/groups", response_model=list[ServerGroupResponse])
async def list_groups(db: DB, _: CurrentUser) -> list[DNSServerGroup]:
    result = await db.execute(select(DNSServerGroup).order_by(DNSServerGroup.name))
    return list(result.scalars().all())


@router.post("/groups", response_model=ServerGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(body: ServerGroupCreate, db: DB, current_user: SuperAdmin) -> DNSServerGroup:
    existing = await db.execute(select(DNSServerGroup).where(DNSServerGroup.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A server group with that name already exists")

    group = DNSServerGroup(**body.model_dump())
    db.add(group)

    # Auto-create default options
    options = DNSServerOptions(group=group)
    db.add(options)

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_server_group",
            resource_id=str(group.id),
            resource_display=group.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(group)
    logger.info("dns_group_created", group_id=str(group.id), name=group.name)
    return group


@router.get("/groups/{group_id}", response_model=ServerGroupResponse)
async def get_group(group_id: uuid.UUID, db: DB, _: CurrentUser) -> DNSServerGroup:
    group = await db.get(DNSServerGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Server group not found")
    return group


@router.put("/groups/{group_id}", response_model=ServerGroupResponse)
async def update_group(
    group_id: uuid.UUID, body: ServerGroupUpdate, db: DB, current_user: SuperAdmin
) -> DNSServerGroup:
    group = await db.get(DNSServerGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Server group not found")

    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(group, k, v)

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_server_group",
            resource_id=str(group.id),
            resource_display=group.name,
            changed_fields=list(changes.keys()),
            result="success",
        )
    )
    await db.commit()
    await db.refresh(group)
    return group


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: uuid.UUID, db: DB, current_user: SuperAdmin) -> None:
    group = await db.get(DNSServerGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Server group not found")

    # The ORM-level ``cascade="all, delete-orphan"`` on zones/servers/views will
    # happily wipe a populated group, which is an easy foot-gun — the user can
    # accidentally nuke a group that still has live zones and registered
    # servers. Pre-check and return 409 with counts so the UI gets a clear
    # error instead of a silent cascade. Matches the IP space / block pattern.
    server_count = (
        await db.execute(
            select(func.count()).select_from(DNSServer).where(DNSServer.group_id == group_id)
        )
    ).scalar_one()
    zone_count = (
        await db.execute(
            select(func.count()).select_from(DNSZone).where(DNSZone.group_id == group_id)
        )
    ).scalar_one()
    if server_count or zone_count:
        parts = []
        if server_count:
            parts.append(f"{server_count} server(s)")
        if zone_count:
            parts.append(f"{zone_count} zone(s)")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"DNS server group {group.name!r} still contains "
                f"{' and '.join(parts)}. Delete or move them before deleting "
                "the group."
            ),
        )

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="dns_server_group",
            resource_id=str(group.id),
            resource_display=group.name,
            result="success",
        )
    )
    await db.delete(group)
    await db.commit()


# ── DNS Server endpoints ────────────────────────────────────────────────────


@router.get("/groups/{group_id}/servers", response_model=list[ServerResponse])
async def list_servers(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[ServerResponse]:
    await _require_group(group_id, db)
    result = await db.execute(
        select(DNSServer).where(DNSServer.group_id == group_id).order_by(DNSServer.name)
    )
    return [ServerResponse.from_model(s) for s in result.scalars().all()]


@router.post(
    "/groups/{group_id}/servers", response_model=ServerResponse, status_code=status.HTTP_201_CREATED
)
async def create_server(
    group_id: uuid.UUID, body: ServerCreate, db: DB, current_user: SuperAdmin
) -> ServerResponse:
    await _require_group(group_id, db)
    existing = await db.execute(
        select(DNSServer).where(DNSServer.group_id == group_id, DNSServer.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A server with that name already exists in this group"
        )

    data = body.model_dump(exclude={"api_key", "windows_credentials"})
    data["group_id"] = group_id
    if body.api_key:
        # TODO: encrypt before storing
        data["api_key_encrypted"] = body.api_key

    # Auto-mark this server as primary if the group has no primary yet.
    # Prevents the footgun where a freshly-created group has zero primaries
    # and `enqueue_record_op` silently drops every write targeting its zones.
    # Admins can still flip the flag later via PUT; this only fires on create.
    has_primary = await db.execute(
        select(DNSServer).where(DNSServer.group_id == group_id, DNSServer.is_primary.is_(True))
    )
    if has_primary.first() is None:
        data["is_primary"] = True

    server = DNSServer(**data)

    # Windows-only optional credential block. Driver-check + both-fields
    # required on first set — mirror DHCP.
    if body.driver == "windows_dns" and body.windows_credentials is not None:
        creds = body.windows_credentials.model_dump(exclude_none=True)
        if not creds.get("username") or not creds.get("password"):
            raise HTTPException(
                status_code=400,
                detail="windows_dns create with credentials requires both username and password",
            )
        creds.setdefault("winrm_port", 5985)
        creds.setdefault("transport", "ntlm")
        creds.setdefault("use_tls", False)
        creds.setdefault("verify_tls", False)
        server.credentials_encrypted = encrypt_dict(creds)

    db.add(server)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_server",
            resource_id=str(server.id),
            resource_display=server.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(server)
    return ServerResponse.from_model(server)


@router.put("/groups/{group_id}/servers/{server_id}", response_model=ServerResponse)
async def update_server(
    group_id: uuid.UUID, server_id: uuid.UUID, body: ServerUpdate, db: DB, current_user: SuperAdmin
) -> ServerResponse:
    server = await _require_server(group_id, server_id, db)
    changes = body.model_dump(exclude_none=True, exclude={"api_key", "windows_credentials"})
    if body.api_key is not None:
        changes["api_key_encrypted"] = body.api_key  # TODO: encrypt
    # When the user flips is_enabled, reflect it in status immediately so
    # the UI pill updates without waiting for the next 60s health-sweep
    # tick. The sweep then re-asserts on schedule.
    if body.is_enabled is not None and body.is_enabled != server.is_enabled:
        if body.is_enabled:
            # Re-enabling → transitional "syncing" state. Next sweep tick
            # will probe and set active/unreachable.
            if server.status == "disabled":
                changes["status"] = "syncing"
        else:
            changes["status"] = "disabled"
    for k, v in changes.items():
        setattr(server, k, v)

    # Credentials contract matches DHCP:
    #   None → leave alone, {} → clear, partial dict → decrypt-merge-reencrypt
    if body.windows_credentials is not None:
        if isinstance(body.windows_credentials, WindowsCredentialsInput):
            patch = body.windows_credentials.model_dump(exclude_none=True)
            if not patch:
                pass  # empty WindowsCredentialsInput — no-op
            elif server.credentials_encrypted:
                existing = decrypt_dict(server.credentials_encrypted)
                existing.update(patch)
                server.credentials_encrypted = encrypt_dict(existing)
                changes["windows_credentials_updated"] = sorted(patch.keys())
            else:
                if not patch.get("username") or not patch.get("password"):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "First-time credentials require both username and "
                            "password (other fields are optional)."
                        ),
                    )
                patch.setdefault("winrm_port", 5985)
                patch.setdefault("transport", "ntlm")
                patch.setdefault("use_tls", False)
                patch.setdefault("verify_tls", False)
                server.credentials_encrypted = encrypt_dict(patch)
                changes["windows_credentials_set"] = True
        elif body.windows_credentials == {}:
            server.credentials_encrypted = None
            changes["windows_credentials_cleared"] = True

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_server",
            resource_id=str(server.id),
            resource_display=server.name,
            changed_fields=list(changes.keys()),
            result="success",
        )
    )
    await db.commit()
    await db.refresh(server)
    return ServerResponse.from_model(server)


@router.delete("/groups/{group_id}/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    group_id: uuid.UUID, server_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> None:
    server = await _require_server(group_id, server_id, db)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="dns_server",
            resource_id=str(server.id),
            resource_display=server.name,
            result="success",
        )
    )
    await db.delete(server)
    await db.commit()


# ── Windows DNS (Path B) — WinRM helpers ────────────────────────────────────


@router.post("/test-windows-credentials", response_model=TestResult)
async def test_windows_credentials_endpoint(
    body: TestWindowsCredentialsRequest, db: DB, _user: SuperAdmin
) -> TestResult:
    """Dry-run WinRM probe — reach the host, run ``Get-DnsServerSetting``.

    Two modes (mirrors the DHCP-side endpoint):
      * **Pre-save** (create/edit form) — pass plaintext ``credentials``
        and the typed ``host``. Nothing is written to the DB.
      * **Post-save** (existing server) — pass ``server_id`` only;
        stored Fernet-encrypted credentials are decrypted and used.
    """
    if body.credentials is not None:
        creds = body.credentials.model_dump(exclude_none=True)
        if not creds.get("username") or not creds.get("password"):
            # Partial patch (e.g. transport-only) → merge with stored
            # credentials if a server_id was also sent, else reject as
            # ambiguous. Same behaviour as the DHCP test endpoint.
            if body.server_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="Partial credentials require 'server_id' to merge with stored",
                )
            srv = await db.get(DNSServer, body.server_id)
            if srv is None:
                raise HTTPException(status_code=404, detail="Server not found")
            if not srv.credentials_encrypted:
                raise HTTPException(
                    status_code=400,
                    detail="Server has no stored credentials to merge against",
                )
            existing = decrypt_dict(srv.credentials_encrypted)
            existing.update(creds)
            creds = existing
        creds.setdefault("winrm_port", 5985)
        creds.setdefault("transport", "ntlm")
        creds.setdefault("use_tls", False)
        creds.setdefault("verify_tls", False)
        host = body.host
    elif body.server_id is not None:
        srv = await db.get(DNSServer, body.server_id)
        if srv is None:
            raise HTTPException(status_code=404, detail="Server not found")
        if not srv.credentials_encrypted:
            raise HTTPException(status_code=400, detail="Server has no stored credentials to test")
        creds = decrypt_dict(srv.credentials_encrypted)
        host = body.host or srv.host
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'credentials' (dry-run) or 'server_id' (stored)",
        )

    ok, msg = await test_winrm_credentials(host, creds)
    return TestResult(ok=ok, message=msg)


@router.post(
    "/groups/{group_id}/servers/{server_id}/pull-zones-from-server",
    response_model=PullZonesResult,
)
async def pull_zones_from_server(
    group_id: uuid.UUID,
    server_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
) -> PullZonesResult:
    """List the zones hosted on a Windows DNS server over WinRM.

    Requires ``driver='windows_dns'`` and stored credentials on the
    server row. Returns the raw zone topology as PowerShell reported it
    — caller decides what to do next (show in UI, import, reconcile).
    """
    server = await _require_server(group_id, server_id, db)
    if server.driver != "windows_dns":
        raise HTTPException(
            status_code=400,
            detail=f"pull-zones is only supported on windows_dns (got {server.driver!r})",
        )
    if not server.credentials_encrypted:
        raise HTTPException(
            status_code=400,
            detail=(
                "This server has no Windows credentials configured. "
                "Add credentials on the server to enable WinRM zone topology reads."
            ),
        )

    from app.drivers.dns.windows import WindowsDNSDriver  # noqa: PLC0415

    driver = WindowsDNSDriver()
    try:
        zones = await driver.pull_zones_from_server(server)
    except Exception as exc:  # noqa: BLE001 — surface PS / WinRM errors verbatim
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="dns.server.pull_zones",
            resource_type="dns_server",
            resource_id=str(server.id),
            resource_display=server.name,
            result="success",
        )
    )
    await db.commit()
    return PullZonesResult(zones=zones)


class ZoneSyncItem(BaseModel):
    """Per-zone result entry in ``SyncFromServerResponse``."""

    zone: str
    imported: int
    pushed: int
    server_records: int
    push_errors: list[str]
    error: str | None = None


class SyncFromServerResponse(BaseModel):
    zones_attempted: int
    zones_succeeded: int
    zones_failed: int
    total_imported: int
    total_pushed: int
    total_push_errors: int
    # Zones listed by the server over WinRM (windows_dns Path B only).
    # Empty for BIND9 and Windows DNS without credentials — the caller can
    # still act on the imported-records count.
    zones_on_server: list[str]
    # Zones present on the server but not tracked in SpatiumDDI. With
    # ``import_new_zones=True`` (the default) these are auto-created in
    # the DB before the AXFR loop runs, so records land in one pass.
    new_zones_on_server: list[str]
    zones_imported: list[str]
    zones_skipped_system: list[str]
    # Zones present in SpatiumDDI but not on the server, that we pushed
    # over WinRM during this sync. Only populated for windows_dns+creds —
    # closes the "I created a zone in SpatiumDDI before the write-through
    # was wired up" drift loop on the first sync after the fact.
    zones_pushed_to_server: list[str] = []
    zones_push_to_server_errors: list[str] = []
    items: list[ZoneSyncItem]


class ServerSyncItem(BaseModel):
    """Per-server result in ``GroupSyncWithServersResponse``."""

    server_id: uuid.UUID
    server_name: str
    driver: str
    error: str | None = None
    result: SyncFromServerResponse | None = None


class GroupSyncWithServersResponse(BaseModel):
    servers_attempted: int
    servers_succeeded: int
    total_imported: int
    total_pushed: int
    total_push_errors: int
    total_zones_imported: int
    total_zones_pushed_to_server: int
    items: list[ServerSyncItem]


# Windows-internal zone names we never want to auto-import. ``TrustAnchors``
# is the DNSSEC trust-anchor store; ``RootHints`` and ``Cache`` are also
# Windows internal. Anything without a dot in the name is suspicious too
# — a real DNS zone always has at least one label separator.
_WINDOWS_SYSTEM_ZONE_NAMES: frozenset[str] = frozenset(
    {"TrustAnchors", "RootHints", "Cache", ".", ""}
)


def _is_system_zone_name(name: str) -> bool:
    bare = (name or "").rstrip(".")
    if bare in _WINDOWS_SYSTEM_ZONE_NAMES:
        return True
    # No-dot "zones" like ``TrustAnchors`` were caught above; this guards
    # future weirdness without blocking single-label experimental setups
    # that operators intentionally added to their DB.
    return "." not in bare


def _infer_zone_kind(name: str, is_reverse: bool | None) -> str:
    bare = (name or "").rstrip(".").lower()
    if is_reverse:
        return "reverse"
    if bare.endswith(".in-addr.arpa") or bare.endswith(".ip6.arpa"):
        return "reverse"
    return "forward"


async def _sync_single_server(
    db: DB,
    server: DNSServer,
    current_user: CurrentUser,
    *,
    import_new_zones: bool = True,
    commit: bool = True,
) -> SyncFromServerResponse:
    """Bi-directional additive sync against a single server.

    Shared core used by both the per-server and group-level endpoints.
    ``commit=False`` lets the group-level caller batch many servers into
    one DB transaction; ``commit=True`` mirrors the classic one-shot
    per-server flow.

    Ordering is load-bearing:
      1. List zones on the server over WinRM (windows_dns Path B only).
      2. Import new zones server→DB if any are missing from our DB.
      3. **Push missing zones DB→server** for windows_dns+creds. Without
         this step, records queued in SpatiumDDI against a zone the
         Windows server has never heard of fail with "zone not found"
         on the first record-push — exactly the joe.com drift we hit.
      4. Run the existing bi-directional per-zone sync for records.
    """
    from app.drivers.dns.windows import WindowsDNSDriver  # noqa: PLC0415
    from app.services.dns.pull_from_server import sync_zone_with_server  # noqa: PLC0415

    group_id = server.group_id

    # 1. Path B discovery.
    zones_on_server: list[str] = []
    zone_meta_by_name: dict[str, dict[str, Any]] = {}
    if server.driver == "windows_dns" and server.credentials_encrypted:
        try:
            winrm_zones = await WindowsDNSDriver().pull_zones_from_server(server)
            for z in winrm_zones:
                name = str(z.get("name") or "").rstrip(".")
                if not name:
                    continue
                zones_on_server.append(name)
                zone_meta_by_name[name] = z
        except Exception as exc:  # noqa: BLE001 — informational, keep going
            logger.warning(
                "dns.sync_from_server.pull_zones_winrm_failed",
                server=str(server.id),
                error=str(exc),
            )

    existing_res = await db.execute(select(DNSZone.name).where(DNSZone.group_id == group_id))
    existing_names = {str(n).rstrip(".") for n in existing_res.scalars().all()}
    new_zones = sorted({n for n in zones_on_server if n not in existing_names})

    # 2. Auto-import server→DB.
    zones_imported: list[str] = []
    zones_skipped_system: list[str] = []
    if import_new_zones and new_zones:
        for name in new_zones:
            if _is_system_zone_name(name):
                zones_skipped_system.append(name)
                continue
            meta = zone_meta_by_name.get(name, {})
            is_reverse = bool(meta.get("is_reverse_lookup"))
            kind = _infer_zone_kind(name, is_reverse)
            zone = DNSZone(
                group_id=group_id,
                name=name if name.endswith(".") else name + ".",
                zone_type="primary",
                kind=kind,
                ttl=3600,
                refresh=86400,
                retry=7200,
                expire=3600000,
                minimum=3600,
                primary_ns="",
                admin_email="",
                dnssec_enabled=False,
            )
            db.add(zone)
            zones_imported.append(name)
            db.add(
                AuditLog(
                    user_id=current_user.id,
                    user_display_name=current_user.display_name,
                    auth_source=current_user.auth_source,
                    action="dns.zone.auto_import_from_server",
                    resource_type="dns_zone",
                    resource_id=str(zone.id),
                    resource_display=zone.name,
                    result="success",
                    new_value={
                        "server_id": str(server.id),
                        "kind": kind,
                        "windows_zone_type": meta.get("zone_type"),
                    },
                )
            )
        if zones_imported:
            await db.flush()

    # 3. Push missing zones DB→server (windows_dns+creds only).
    zones_pushed_to_server: list[str] = []
    zones_push_to_server_errors: list[str] = []
    if server.driver == "windows_dns" and server.credentials_encrypted:
        server_zone_set = {n for n in zones_on_server}
        driver = WindowsDNSDriver()
        # Re-query zones after the import step so newly-imported rows
        # are excluded from the "missing on server" set automatically.
        db_zones_res = await db.execute(select(DNSZone).where(DNSZone.group_id == group_id))
        for zone in db_zones_res.scalars().all():
            bare = zone.name.rstrip(".")
            if bare in server_zone_set:
                continue
            try:
                await driver.apply_zone_change(server, zone, "create")
                zones_pushed_to_server.append(bare)
                db.add(
                    AuditLog(
                        user_id=current_user.id,
                        user_display_name=current_user.display_name,
                        auth_source=current_user.auth_source,
                        action="dns.zone.push_missing_to_server",
                        resource_type="dns_zone",
                        resource_id=str(zone.id),
                        resource_display=zone.name,
                        result="success",
                        new_value={"server_id": str(server.id)},
                    )
                )
            except Exception as exc:  # noqa: BLE001 — per-zone isolation
                zones_push_to_server_errors.append(f"{bare}: {exc}")
                logger.warning(
                    "dns.sync_from_server.zone_push_failed",
                    server=str(server.id),
                    zone=zone.name,
                    error=str(exc),
                )

    # 4. Per-zone record sync.
    zones_res = await db.execute(
        select(DNSZone).where(DNSZone.group_id == group_id).order_by(DNSZone.name)
    )
    zones = list(zones_res.scalars().all())

    items: list[ZoneSyncItem] = []
    zones_succeeded = 0
    total_imported = 0
    total_pushed = 0
    total_push_errors = 0

    for zone in zones:
        try:
            result = await sync_zone_with_server(db, zone, apply=True)
            zones_succeeded += 1
            total_imported += result.pull.imported
            total_pushed += result.push.pushed
            total_push_errors += len(result.push.push_errors)
            items.append(
                ZoneSyncItem(
                    zone=zone.name.rstrip("."),
                    imported=result.pull.imported,
                    pushed=result.push.pushed,
                    server_records=result.pull.server_records,
                    push_errors=result.push.push_errors,
                )
            )
        except Exception as exc:  # noqa: BLE001 — per-zone isolation
            items.append(
                ZoneSyncItem(
                    zone=zone.name.rstrip("."),
                    imported=0,
                    pushed=0,
                    server_records=0,
                    push_errors=[],
                    error=str(exc),
                )
            )
            logger.warning(
                "dns.sync_from_server.zone_failed",
                server=str(server.id),
                zone=zone.name,
                error=str(exc),
            )

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="dns.server.sync_from_server",
            resource_type="dns_server",
            resource_id=str(server.id),
            resource_display=server.name,
            result=(
                "error"
                if total_push_errors or zones_push_to_server_errors or any(i.error for i in items)
                else "success"
            ),
            new_value={
                "zones_attempted": len(zones),
                "zones_succeeded": zones_succeeded,
                "zones_auto_imported": zones_imported,
                "zones_skipped_system": zones_skipped_system,
                "zones_pushed_to_server": zones_pushed_to_server,
                "zones_push_to_server_errors": zones_push_to_server_errors,
                "total_imported": total_imported,
                "total_pushed": total_pushed,
                "total_push_errors": total_push_errors,
                "new_zones_on_server": new_zones,
            },
        )
    )
    if commit:
        await db.commit()

    return SyncFromServerResponse(
        zones_attempted=len(zones),
        zones_succeeded=zones_succeeded,
        zones_failed=len(zones) - zones_succeeded,
        total_imported=total_imported,
        total_pushed=total_pushed,
        total_push_errors=total_push_errors,
        zones_on_server=sorted(set(zones_on_server)),
        new_zones_on_server=new_zones,
        zones_imported=zones_imported,
        zones_skipped_system=zones_skipped_system,
        zones_pushed_to_server=zones_pushed_to_server,
        zones_push_to_server_errors=zones_push_to_server_errors,
        items=items,
    )


@router.post(
    "/groups/{group_id}/servers/{server_id}/sync-from-server",
    response_model=SyncFromServerResponse,
)
async def sync_server_from_server(
    group_id: uuid.UUID,
    server_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
    import_new_zones: bool = True,
) -> SyncFromServerResponse:
    """Bi-directional sync with a single server. See ``_sync_single_server``."""
    server = await _require_server(group_id, server_id, db)
    return await _sync_single_server(
        db, server, current_user, import_new_zones=import_new_zones, commit=True
    )


@router.post(
    "/groups/{group_id}/sync-with-servers",
    response_model=GroupSyncWithServersResponse,
)
async def sync_group_with_servers(
    group_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
    import_new_zones: bool = True,
) -> GroupSyncWithServersResponse:
    """Bi-directional sync against every enabled server in a group.

    One button, one round-trip per server. Per-server failure is isolated
    — a bad DC doesn't abort the sync for the other DCs in the group —
    and the response carries a per-server breakdown.

    The same write-through contract applies: windows_dns+creds servers
    get their missing zones auto-pushed from SpatiumDDI, so zones
    created here before credentials landed still converge on this click.
    """
    await _require_group(group_id, db)
    servers_res = await db.execute(
        select(DNSServer)
        .where(DNSServer.group_id == group_id, DNSServer.is_enabled.is_(True))
        .order_by(DNSServer.name)
    )
    servers = list(servers_res.scalars().all())

    items: list[ServerSyncItem] = []
    servers_succeeded = 0
    total_imported = 0
    total_pushed = 0
    total_push_errors = 0
    total_zones_imported = 0
    total_zones_pushed_to_server = 0

    for server in servers:
        try:
            result = await _sync_single_server(
                db,
                server,
                current_user,
                import_new_zones=import_new_zones,
                commit=False,
            )
            items.append(
                ServerSyncItem(
                    server_id=server.id,
                    server_name=server.name,
                    driver=server.driver,
                    result=result,
                )
            )
            servers_succeeded += 1
            total_imported += result.total_imported
            total_pushed += result.total_pushed
            total_push_errors += result.total_push_errors
            total_zones_imported += len(result.zones_imported)
            total_zones_pushed_to_server += len(result.zones_pushed_to_server)
        except Exception as exc:  # noqa: BLE001 — per-server isolation
            items.append(
                ServerSyncItem(
                    server_id=server.id,
                    server_name=server.name,
                    driver=server.driver,
                    error=str(exc),
                )
            )
            logger.warning(
                "dns.group.sync_with_servers.server_failed",
                group=str(group_id),
                server=str(server.id),
                error=str(exc),
            )

    await db.commit()

    return GroupSyncWithServersResponse(
        servers_attempted=len(servers),
        servers_succeeded=servers_succeeded,
        total_imported=total_imported,
        total_pushed=total_pushed,
        total_push_errors=total_push_errors,
        total_zones_imported=total_zones_imported,
        total_zones_pushed_to_server=total_zones_pushed_to_server,
        items=items,
    )


# ── Incremental record push (per DNS_AGENT.md §3) ──────────────────────────
#
# The control plane records the intent (serial bump + last_pushed_at) and
# returns 202. Actual loopback nsupdate to ``named`` happens inside the
# agent when it pulls the ConfigBundle — the control plane never speaks
# RFC 2136 over the network itself.


class ApplyRecordRequest(BaseModel):
    zone_id: uuid.UUID
    record_id: uuid.UUID
    op: str  # create | update | delete

    @field_validator("op")
    @classmethod
    def _validate_op(cls, v: str) -> str:
        if v not in {"create", "update", "delete"}:
            raise ValueError("op must be one of create|update|delete")
        return v


class ApplyRecordResponse(BaseModel):
    server_id: uuid.UUID
    zone_id: uuid.UUID
    record_id: uuid.UUID
    op: str
    target_serial: int
    accepted_at: datetime


@router.post(
    "/servers/{server_id}/apply-record",
    response_model=ApplyRecordResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_record(
    server_id: uuid.UUID,
    body: ApplyRecordRequest,
    db: DB,
    current_user: SuperAdmin,
) -> ApplyRecordResponse:
    """Record a per-record push intent for a DNS server.

    The control plane bumps the zone serial, marks the zone ``last_pushed_at``
    now, and returns 202. The agent running alongside the daemon picks this up
    through its long-poll config channel and executes the matching loopback
    nsupdate via RFC 2136.
    """
    server = await db.get(DNSServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="DNS server not found")
    zone = await db.get(DNSZone, body.zone_id)
    if zone is None or zone.group_id != server.group_id:
        raise HTTPException(status_code=404, detail="Zone not found on this server's group")

    if body.op != "delete":
        record = await db.get(DNSRecord, body.record_id)
        if record is None or record.zone_id != zone.id:
            raise HTTPException(status_code=404, detail="Record not found in zone")

    now = datetime.now(UTC)
    target_serial = bump_zone_serial(zone)
    zone.last_pushed_at = now

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="apply_record",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=zone.name,
            new_value={
                "server_id": str(server.id),
                "record_id": str(body.record_id),
                "op": body.op,
                "target_serial": target_serial,
            },
            result="success",
        )
    )
    await db.commit()

    return ApplyRecordResponse(
        server_id=server.id,
        zone_id=zone.id,
        record_id=body.record_id,
        op=body.op,
        target_serial=target_serial,
        accepted_at=now,
    )


# ── Server Options endpoints ────────────────────────────────────────────────


async def _load_options(group_id: uuid.UUID, db: DB) -> DNSServerOptions | None:
    result = await db.execute(
        select(DNSServerOptions)
        .where(DNSServerOptions.group_id == group_id)
        .options(selectinload(DNSServerOptions.trust_anchors))
    )
    return result.scalar_one_or_none()


@router.get("/groups/{group_id}/options", response_model=ServerOptionsResponse)
async def get_options(group_id: uuid.UUID, db: DB, _: CurrentUser) -> DNSServerOptions:
    await _require_group(group_id, db)
    opts = await _load_options(group_id, db)
    if not opts:
        # Auto-create defaults on first access
        opts = DNSServerOptions(group_id=group_id)
        db.add(opts)
        await db.commit()
        opts = await _load_options(group_id, db)
    return opts  # type: ignore[return-value]


@router.put("/groups/{group_id}/options", response_model=ServerOptionsResponse)
async def update_options(
    group_id: uuid.UUID, body: ServerOptionsUpdate, db: DB, current_user: SuperAdmin
) -> DNSServerOptions:
    await _require_group(group_id, db)
    opts = await _load_options(group_id, db)
    if not opts:
        opts = DNSServerOptions(group_id=group_id)
        db.add(opts)

    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(opts, k, v)

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_server_options",
            resource_id=str(opts.id) if opts.id else "new",
            resource_display=f"options for group {group_id}",
            changed_fields=list(changes.keys()),
            result="success",
        )
    )
    await db.commit()
    reloaded = await _load_options(group_id, db)
    return reloaded  # type: ignore[return-value]


@router.post(
    "/groups/{group_id}/options/trust-anchors", response_model=TrustAnchorResponse, status_code=201
)
async def add_trust_anchor(
    group_id: uuid.UUID, body: TrustAnchorCreate, db: DB, current_user: SuperAdmin
) -> DNSTrustAnchor:
    await _require_group(group_id, db)
    result = await db.execute(select(DNSServerOptions).where(DNSServerOptions.group_id == group_id))
    opts = result.scalar_one_or_none()
    if not opts:
        opts = DNSServerOptions(group_id=group_id)
        db.add(opts)
        await db.flush()

    anchor = DNSTrustAnchor(
        **body.model_dump(),
        server_options_id=opts.id,
        added_at=datetime.now(UTC),
        added_by_user_id=current_user.id,
    )
    db.add(anchor)
    await db.commit()
    await db.refresh(anchor)
    return anchor


@router.delete("/groups/{group_id}/options/trust-anchors/{anchor_id}", status_code=204)
async def delete_trust_anchor(
    group_id: uuid.UUID, anchor_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> None:
    await _require_group(group_id, db)
    result = await db.execute(
        select(DNSTrustAnchor)
        .join(DNSServerOptions)
        .where(DNSServerOptions.group_id == group_id, DNSTrustAnchor.id == anchor_id)
    )
    anchor = result.scalar_one_or_none()
    if not anchor:
        raise HTTPException(status_code=404, detail="Trust anchor not found")
    await db.delete(anchor)
    await db.commit()


# ── ACL endpoints ───────────────────────────────────────────────────────────


def _acl_query(group_id: uuid.UUID):  # type: ignore[no-untyped-def]
    return (
        select(DNSAcl)
        .where(DNSAcl.group_id == group_id)
        .options(selectinload(DNSAcl.entries))
        .order_by(DNSAcl.name)
    )


async def _load_acl(group_id: uuid.UUID, acl_id: uuid.UUID, db: DB) -> DNSAcl | None:
    result = await db.execute(
        select(DNSAcl)
        .where(DNSAcl.id == acl_id, DNSAcl.group_id == group_id)
        .options(selectinload(DNSAcl.entries))
    )
    return result.scalar_one_or_none()


@router.get("/groups/{group_id}/acls", response_model=list[AclResponse])
async def list_acls(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[DNSAcl]:
    await _require_group(group_id, db)
    result = await db.execute(_acl_query(group_id))
    return list(result.scalars().all())


@router.post("/groups/{group_id}/acls", response_model=AclResponse, status_code=201)
async def create_acl(
    group_id: uuid.UUID, body: AclCreate, db: DB, current_user: SuperAdmin
) -> DNSAcl:
    await _require_group(group_id, db)
    existing = await db.execute(
        select(DNSAcl).where(DNSAcl.group_id == group_id, DNSAcl.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="An ACL with that name already exists in this group"
        )

    acl = DNSAcl(group_id=group_id, name=body.name, description=body.description)
    db.add(acl)
    await db.flush()

    for e in body.entries:
        db.add(DNSAclEntry(acl_id=acl.id, **e.model_dump()))

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_acl",
            resource_id=str(acl.id),
            resource_display=acl.name,
            result="success",
        )
    )
    await db.commit()
    return await _load_acl(group_id, acl.id, db)  # type: ignore[return-value]


@router.put("/groups/{group_id}/acls/{acl_id}", response_model=AclResponse)
async def update_acl(
    group_id: uuid.UUID, acl_id: uuid.UUID, body: AclUpdate, db: DB, current_user: SuperAdmin
) -> DNSAcl:
    acl = await _require_acl(group_id, acl_id, db)
    changes = body.model_dump(exclude_none=True, exclude={"entries"})
    for k, v in changes.items():
        setattr(acl, k, v)

    if body.entries is not None:
        result = await db.execute(select(DNSAclEntry).where(DNSAclEntry.acl_id == acl.id))
        for entry in result.scalars().all():
            await db.delete(entry)
        await db.flush()
        for e in body.entries:
            db.add(DNSAclEntry(acl_id=acl.id, **e.model_dump()))

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_acl",
            resource_id=str(acl.id),
            resource_display=acl.name,
            result="success",
        )
    )
    await db.commit()
    return await _load_acl(group_id, acl_id, db)  # type: ignore[return-value]


@router.delete("/groups/{group_id}/acls/{acl_id}", status_code=204)
async def delete_acl(
    group_id: uuid.UUID, acl_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> None:
    acl = await _require_acl(group_id, acl_id, db)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="dns_acl",
            resource_id=str(acl.id),
            resource_display=acl.name,
            result="success",
        )
    )
    await db.delete(acl)
    await db.commit()


# ── TSIG key endpoints ──────────────────────────────────────────────────────


_TSIG_ALGO_LENGTHS = {
    "hmac-sha1": 20,
    "hmac-sha224": 28,
    "hmac-sha256": 32,
    "hmac-sha384": 48,
    "hmac-sha512": 64,
}
_TSIG_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-]*[a-z0-9])?\.?$")


def _generate_tsig_secret(algorithm: str) -> str:
    import base64
    import secrets

    nbytes = _TSIG_ALGO_LENGTHS.get(algorithm.lower(), 32)
    return base64.b64encode(secrets.token_bytes(nbytes)).decode("ascii")


class TSIGKeyCreate(BaseModel):
    name: str
    algorithm: str = "hmac-sha256"
    # Empty/missing secret → generate one server-side. Operator-supplied
    # secrets must already be base64.
    secret: str | None = None
    purpose: str | None = None
    notes: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not _TSIG_NAME_RE.match(v):
            raise ValueError(
                "name must be a dotted-ASCII label (a-z, 0-9, '.', '-'); e.g. tsig-update.spatium.local."
            )
        return v

    @field_validator("algorithm")
    @classmethod
    def validate_algo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _TSIG_ALGO_LENGTHS:
            raise ValueError(f"algorithm must be one of {sorted(_TSIG_ALGO_LENGTHS)}")
        return v


class TSIGKeyUpdate(BaseModel):
    name: str | None = None
    algorithm: str | None = None
    purpose: str | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not _TSIG_NAME_RE.match(v):
            raise ValueError("name must be a dotted-ASCII label")
        return v

    @field_validator("algorithm")
    @classmethod
    def validate_algo(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in _TSIG_ALGO_LENGTHS:
            raise ValueError(f"algorithm must be one of {sorted(_TSIG_ALGO_LENGTHS)}")
        return v


class TSIGKeyResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    name: str
    algorithm: str
    purpose: str | None
    notes: str
    last_rotated_at: datetime | None
    created_at: datetime
    modified_at: datetime
    # Plaintext secret. Populated *only* on the create / rotate responses.
    # List + get endpoints return null so secrets never persist outside
    # postgres in plaintext.
    secret: str | None = None

    model_config = {"from_attributes": True}


class TSIGGenerateSecretResponse(BaseModel):
    algorithm: str
    secret: str


@router.get(
    "/groups/{group_id}/tsig-keys/generate-secret",
    response_model=TSIGGenerateSecretResponse,
)
async def generate_tsig_secret(
    group_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    algorithm: str = "hmac-sha256",
) -> TSIGGenerateSecretResponse:
    """Return a freshly-generated random base64 secret of the right size.

    Helper for the create-form pre-fill. No DB write — the operator can
    discard or regenerate before submitting.
    """
    await _require_group(group_id, db)
    algo = algorithm.strip().lower()
    if algo not in _TSIG_ALGO_LENGTHS:
        raise HTTPException(status_code=422, detail=f"unsupported algorithm: {algorithm}")
    return TSIGGenerateSecretResponse(algorithm=algo, secret=_generate_tsig_secret(algo))


@router.get("/groups/{group_id}/tsig-keys", response_model=list[TSIGKeyResponse])
async def list_tsig_keys(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[TSIGKeyResponse]:
    await _require_group(group_id, db)
    res = await db.execute(
        select(DNSTSIGKey).where(DNSTSIGKey.group_id == group_id).order_by(DNSTSIGKey.name)
    )
    keys = list(res.scalars().all())
    return [TSIGKeyResponse.model_validate(k) for k in keys]


@router.post(
    "/groups/{group_id}/tsig-keys",
    response_model=TSIGKeyResponse,
    status_code=201,
)
async def create_tsig_key(
    group_id: uuid.UUID,
    body: TSIGKeyCreate,
    db: DB,
    current_user: SuperAdmin,
) -> TSIGKeyResponse:
    """Create a TSIG key. Secret is returned in the response *exactly once*."""
    await _require_group(group_id, db)
    existing = await db.execute(
        select(DNSTSIGKey).where(DNSTSIGKey.group_id == group_id, DNSTSIGKey.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A TSIG key with that name already exists in this group"
        )

    plaintext = body.secret.strip() if body.secret else _generate_tsig_secret(body.algorithm)
    key = DNSTSIGKey(
        group_id=group_id,
        name=body.name,
        algorithm=body.algorithm,
        secret_encrypted=encrypt_str(plaintext),
        purpose=body.purpose,
        notes=body.notes,
    )
    db.add(key)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_tsig_key",
            resource_id=str(key.id),
            resource_display=f"{key.name} ({key.algorithm})",
            result="success",
        )
    )
    await db.commit()
    await db.refresh(key)
    out = TSIGKeyResponse.model_validate(key)
    out.secret = plaintext
    return out


@router.get("/groups/{group_id}/tsig-keys/{key_id}", response_model=TSIGKeyResponse)
async def get_tsig_key(
    group_id: uuid.UUID, key_id: uuid.UUID, db: DB, _: CurrentUser
) -> TSIGKeyResponse:
    key = await db.get(DNSTSIGKey, key_id)
    if key is None or key.group_id != group_id:
        raise HTTPException(status_code=404, detail="TSIG key not found")
    return TSIGKeyResponse.model_validate(key)


@router.put("/groups/{group_id}/tsig-keys/{key_id}", response_model=TSIGKeyResponse)
async def update_tsig_key(
    group_id: uuid.UUID,
    key_id: uuid.UUID,
    body: TSIGKeyUpdate,
    db: DB,
    current_user: SuperAdmin,
) -> TSIGKeyResponse:
    """Update metadata (name / algorithm / purpose / notes). Does not rotate the
    secret — use /rotate for that, since rotating returns the new secret in
    the response body."""
    key = await db.get(DNSTSIGKey, key_id)
    if key is None or key.group_id != group_id:
        raise HTTPException(status_code=404, detail="TSIG key not found")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("name") and changes["name"] != key.name:
        clash = await db.execute(
            select(DNSTSIGKey).where(
                DNSTSIGKey.group_id == group_id,
                DNSTSIGKey.name == changes["name"],
                DNSTSIGKey.id != key_id,
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Name already in use")
    for k, v in changes.items():
        setattr(key, k, v)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_tsig_key",
            resource_id=str(key.id),
            resource_display=key.name,
            changed_fields=list(changes.keys()),
            result="success",
        )
    )
    await db.commit()
    await db.refresh(key)
    return TSIGKeyResponse.model_validate(key)


@router.post("/groups/{group_id}/tsig-keys/{key_id}/rotate", response_model=TSIGKeyResponse)
async def rotate_tsig_key(
    group_id: uuid.UUID, key_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> TSIGKeyResponse:
    """Generate a fresh secret of the same algorithm and replace the stored
    one. Returns the new secret in the response body — show it to the
    operator once and let them copy it into consuming clients."""
    key = await db.get(DNSTSIGKey, key_id)
    if key is None or key.group_id != group_id:
        raise HTTPException(status_code=404, detail="TSIG key not found")
    plaintext = _generate_tsig_secret(key.algorithm)
    key.secret_encrypted = encrypt_str(plaintext)
    key.last_rotated_at = datetime.now(UTC)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="rotate",
            resource_type="dns_tsig_key",
            resource_id=str(key.id),
            resource_display=key.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(key)
    out = TSIGKeyResponse.model_validate(key)
    out.secret = plaintext
    return out


@router.delete("/groups/{group_id}/tsig-keys/{key_id}", status_code=204)
async def delete_tsig_key(
    group_id: uuid.UUID, key_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> None:
    key = await db.get(DNSTSIGKey, key_id)
    if key is None or key.group_id != group_id:
        raise HTTPException(status_code=404, detail="TSIG key not found")
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="dns_tsig_key",
            resource_id=str(key.id),
            resource_display=key.name,
            result="success",
        )
    )
    await db.delete(key)
    await db.commit()


# ── View endpoints ──────────────────────────────────────────────────────────


@router.get("/groups/{group_id}/views", response_model=list[ViewResponse])
async def list_views(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[DNSView]:
    await _require_group(group_id, db)
    result = await db.execute(
        select(DNSView).where(DNSView.group_id == group_id).order_by(DNSView.order, DNSView.name)
    )
    return list(result.scalars().all())


@router.post("/groups/{group_id}/views", response_model=ViewResponse, status_code=201)
async def create_view(
    group_id: uuid.UUID, body: ViewCreate, db: DB, current_user: SuperAdmin
) -> DNSView:
    await _require_group(group_id, db)
    existing = await db.execute(
        select(DNSView).where(DNSView.group_id == group_id, DNSView.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A view with that name already exists in this group"
        )

    view = DNSView(group_id=group_id, **body.model_dump())
    db.add(view)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_view",
            resource_id=str(view.id),
            resource_display=view.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(view)
    return view


@router.put("/groups/{group_id}/views/{view_id}", response_model=ViewResponse)
async def update_view(
    group_id: uuid.UUID, view_id: uuid.UUID, body: ViewUpdate, db: DB, current_user: SuperAdmin
) -> DNSView:
    view = await _require_view(group_id, view_id, db)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(view, k, v)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_view",
            resource_id=str(view.id),
            resource_display=view.name,
            changed_fields=list(changes.keys()),
            result="success",
        )
    )
    await db.commit()
    await db.refresh(view)
    return view


@router.delete("/groups/{group_id}/views/{view_id}", status_code=204)
async def delete_view(
    group_id: uuid.UUID, view_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> None:
    view = await _require_view(group_id, view_id, db)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="dns_view",
            resource_id=str(view.id),
            resource_display=view.name,
            result="success",
        )
    )
    await db.delete(view)
    await db.commit()


# ── Zone endpoints ──────────────────────────────────────────────────────────


@router.get("/groups/{group_id}/zones", response_model=list[ZoneResponse])
async def list_zones(
    group_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    customer_id: uuid.UUID | None = None,
    tag: list[str] = Query(default_factory=list),
) -> list[DNSZone]:
    await _require_group(group_id, db)
    stmt = select(DNSZone).where(DNSZone.group_id == group_id).order_by(DNSZone.name)
    if customer_id is not None:
        stmt = stmt.where(DNSZone.customer_id == customer_id)
    stmt = apply_tag_filter(stmt, DNSZone.tags, tag)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/groups/{group_id}/zones", response_model=ZoneResponse, status_code=201)
async def create_zone(
    group_id: uuid.UUID, body: ZoneCreate, db: DB, current_user: SuperAdmin
) -> DNSZone:
    await _require_group(group_id, db)
    existing = await db.execute(
        select(DNSZone).where(
            DNSZone.group_id == group_id,
            DNSZone.view_id == body.view_id,
            DNSZone.name == body.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A zone with that name already exists in this group/view"
        )

    zone = DNSZone(group_id=group_id, **body.model_dump())
    db.add(zone)

    # Write-through: push the create to any windows_dns-with-creds server
    # in this group *before* we commit. If the Windows push fails we don't
    # want an orphan DB row claiming a zone the authoritative server has
    # never heard of. BIND9 zones still get applied via the agent's next
    # ConfigBundle poll — that's a separate path and untouched here.
    await _push_zone_to_agentless_servers(db, zone, "create")

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=zone.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(zone)
    return zone


@router.get("/groups/{group_id}/zones/export")
async def export_all_zones(
    group_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    view_id: uuid.UUID | None = None,
) -> StreamingResponse:
    """Return all zones in a group (optionally filtered by view) as a zip.

    Registered *before* the parametric ``/zones/{zone_id}`` routes below so
    FastAPI matches the static ``export`` literal; otherwise the request
    falls into the UUID-typed ``{zone_id}`` route and returns 422.
    """
    await _require_group(group_id, db)

    stmt = select(DNSZone).where(DNSZone.group_id == group_id)
    if view_id is not None:
        stmt = stmt.where(DNSZone.view_id == view_id)
    result = await db.execute(stmt.order_by(DNSZone.name))
    zones = list(result.scalars().all())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for zone in zones:
            records = await _load_zone_records(zone.id, db)
            text = write_zone_file(zone, records)
            zf.writestr(zone.name.rstrip(".") + ".zone", text)
    buf.seek(0)

    # UTC timestamp so repeated exports on the same day don't overwrite
    # and file managers sort them in order.
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"dns-zones-{group_id}-{ts}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/groups/{group_id}/zones/{zone_id}", response_model=ZoneResponse)
async def get_zone(group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, _: CurrentUser) -> DNSZone:
    return await _require_zone(group_id, zone_id, db)


class ServerZoneStateEntry(BaseModel):
    server_id: uuid.UUID
    server_name: str
    server_status: str
    current_serial: int | None
    reported_at: datetime | None


class ServerZoneStateResponse(BaseModel):
    zone_id: uuid.UUID
    zone_name: str
    target_serial: int
    servers: list[ServerZoneStateEntry]
    in_sync: bool


@router.get(
    "/groups/{group_id}/zones/{zone_id}/server-state",
    response_model=ServerZoneStateResponse,
)
async def get_zone_server_state(
    group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, _: CurrentUser
) -> ServerZoneStateResponse:
    """Per-server serial snapshot for a zone.

    Returns one entry per server in the zone's group, joined with the
    latest ``DNSServerZoneState.current_serial`` that server reported.
    Servers with no state row yet report ``current_serial=None`` — the
    agent hasn't applied a bundle containing this zone yet. ``in_sync``
    is True when every server that's reported matches the zone's
    target serial (``DNSZone.last_serial``).
    """
    from app.models.dns import DNSServer, DNSServerZoneState  # noqa: PLC0415

    zone = await _require_zone(group_id, zone_id, db)

    # All servers in the group.
    servers_res = await db.execute(
        select(DNSServer).where(DNSServer.group_id == group_id).order_by(DNSServer.name)
    )
    servers = list(servers_res.scalars().all())

    # Latest state for each server/zone pair.
    state_res = await db.execute(
        select(DNSServerZoneState).where(DNSServerZoneState.zone_id == zone_id)
    )
    state_by_server: dict[uuid.UUID, DNSServerZoneState] = {
        s.server_id: s for s in state_res.scalars().all()
    }

    entries: list[ServerZoneStateEntry] = []
    all_in_sync = True
    target = int(zone.last_serial or 0)
    any_reported = False
    for srv in servers:
        st = state_by_server.get(srv.id)
        current = int(st.current_serial) if st else None
        reported = st.reported_at if st else None
        entries.append(
            ServerZoneStateEntry(
                server_id=srv.id,
                server_name=srv.name,
                server_status=srv.status,
                current_serial=current,
                reported_at=reported,
            )
        )
        if current is None:
            all_in_sync = False
        else:
            any_reported = True
            if current != target:
                all_in_sync = False

    return ServerZoneStateResponse(
        zone_id=zone.id,
        zone_name=zone.name,
        target_serial=target,
        servers=entries,
        in_sync=all_in_sync and any_reported,
    )


# ── Per-server detail endpoints (powering the Server Detail modal) ──


class PerServerZoneStateEntry(BaseModel):
    zone_id: uuid.UUID
    zone_name: str
    zone_type: str
    target_serial: int
    current_serial: int | None
    reported_at: datetime | None
    in_sync: bool


class PerServerZoneStateResponse(BaseModel):
    server_id: uuid.UUID
    server_name: str
    zones: list[PerServerZoneStateEntry]
    summary: dict[str, int]


@router.get(
    "/servers/{server_id}/zone-state",
    response_model=PerServerZoneStateResponse,
)
async def get_server_zone_state(
    server_id: uuid.UUID, db: DB, _: CurrentUser
) -> PerServerZoneStateResponse:
    """Per-zone state from this server's perspective.

    For every zone in the server's group, joins the zone's
    ``last_serial`` (target) with this server's
    ``DNSServerZoneState.current_serial`` (what the agent reported).
    Drives the "Zones" tab on the Server Detail modal.
    """
    from app.models.dns import DNSServerZoneState  # noqa: PLC0415

    server = await db.get(DNSServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    zones_res = await db.execute(
        select(DNSZone).where(DNSZone.group_id == server.group_id).order_by(DNSZone.name)
    )
    zones = list(zones_res.scalars().all())

    state_res = await db.execute(
        select(DNSServerZoneState).where(DNSServerZoneState.server_id == server_id)
    )
    state_by_zone: dict[uuid.UUID, DNSServerZoneState] = {
        s.zone_id: s for s in state_res.scalars().all()
    }

    entries: list[PerServerZoneStateEntry] = []
    in_sync_count = 0
    drift_count = 0
    not_reported_count = 0
    for z in zones:
        st = state_by_zone.get(z.id)
        target = int(z.last_serial or 0)
        current = int(st.current_serial) if st else None
        in_sync = current is not None and current == target
        if current is None:
            not_reported_count += 1
        elif in_sync:
            in_sync_count += 1
        else:
            drift_count += 1
        entries.append(
            PerServerZoneStateEntry(
                zone_id=z.id,
                zone_name=z.name,
                zone_type=z.zone_type or "primary",
                target_serial=target,
                current_serial=current,
                reported_at=st.reported_at if st else None,
                in_sync=in_sync,
            )
        )

    return PerServerZoneStateResponse(
        server_id=server.id,
        server_name=server.name,
        zones=entries,
        summary={
            "total": len(entries),
            "in_sync": in_sync_count,
            "drift": drift_count,
            "not_reported": not_reported_count,
        },
    )


class PendingOpEntry(BaseModel):
    op_id: uuid.UUID
    zone_name: str
    op: str
    state: str
    record: dict[str, Any]
    target_serial: int | None
    attempts: int
    last_error: str | None
    created_at: datetime
    applied_at: datetime | None


class PendingOpsResponse(BaseModel):
    server_id: uuid.UUID
    counts: dict[str, int]
    items: list[PendingOpEntry]


@router.get(
    "/servers/{server_id}/pending-ops",
    response_model=PendingOpsResponse,
)
async def get_server_pending_ops(
    server_id: uuid.UUID, db: DB, _: CurrentUser, limit: int = 50
) -> PendingOpsResponse:
    """Pending / in-flight / recently-applied / failed record ops.

    Drives the Server Detail modal's "Sync" tab. The counts dict has
    one key per state value (``pending``, ``in_flight``, ``applied``,
    ``failed``). Items are ordered by ``created_at DESC`` and capped
    at ``limit``.
    """
    from app.models.dns import DNSRecordOp  # noqa: PLC0415

    server = await db.get(DNSServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    counts_res = await db.execute(
        select(DNSRecordOp.state, func.count())
        .where(DNSRecordOp.server_id == server_id)
        .group_by(DNSRecordOp.state)
    )
    counts: dict[str, int] = {row[0]: int(row[1]) for row in counts_res.all()}

    ops_res = await db.execute(
        select(DNSRecordOp)
        .where(DNSRecordOp.server_id == server_id)
        .order_by(DNSRecordOp.created_at.desc())
        .limit(limit)
    )
    items = [
        PendingOpEntry(
            op_id=op.id,
            zone_name=op.zone_name,
            op=op.op,
            state=op.state,
            record=dict(op.record or {}),
            target_serial=op.target_serial,
            attempts=op.attempts,
            last_error=op.last_error,
            created_at=op.created_at,
            applied_at=op.applied_at,
        )
        for op in ops_res.scalars().all()
    ]
    return PendingOpsResponse(
        server_id=server.id,
        counts=counts,
        items=items,
    )


class ServerEventEntry(BaseModel):
    id: str
    timestamp: datetime
    user_display_name: str
    action: str
    resource_type: str
    resource_display: str
    result: str

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id(cls, v: object) -> str:
        return str(v)


class ServerEventsResponse(BaseModel):
    server_id: uuid.UUID
    items: list[ServerEventEntry]


@router.get(
    "/servers/{server_id}/recent-events",
    response_model=ServerEventsResponse,
)
async def get_server_recent_events(
    server_id: uuid.UUID, db: DB, _: CurrentUser, limit: int = 50
) -> ServerEventsResponse:
    """Audit-log rows where ``resource_id`` matches this server.

    The audit log keys ``resource_id`` as text, so we filter on the
    string form of the UUID. Drives the "Events" tab on the Server
    Detail modal.
    """
    server = await db.get(DNSServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.resource_id == str(server_id))
                .order_by(AuditLog.timestamp.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        ServerEventEntry(
            id=str(r.id),
            timestamp=r.timestamp,
            user_display_name=r.user_display_name,
            action=r.action,
            resource_type=r.resource_type,
            resource_display=r.resource_display,
            result=r.result,
        )
        for r in rows
    ]
    return ServerEventsResponse(server_id=server.id, items=items)


# ── Server runtime-state read endpoints (rendered config + rndc) ─────


class RenderedConfigFileEntry(BaseModel):
    path: str
    content: str


class RenderedConfigResponse(BaseModel):
    server_id: uuid.UUID
    rendered_at: datetime | None
    files: list[RenderedConfigFileEntry]


@router.get(
    "/servers/{server_id}/rendered-config",
    response_model=RenderedConfigResponse,
)
async def get_server_rendered_config(
    server_id: uuid.UUID, db: DB, _: CurrentUser
) -> RenderedConfigResponse:
    """Latest agent-pushed snapshot of the on-disk rendered config.

    BIND9 only — Windows DNS has no equivalent on-disk config. Returns
    an empty file list with ``rendered_at=None`` when the agent hasn't
    pushed yet (fresh server, never reloaded). The UI shows a "no
    snapshot yet" placeholder in that case.
    """
    server = await db.get(DNSServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    state = await db.get(DNSServerRuntimeState, server_id)
    files: list[RenderedConfigFileEntry] = []
    rendered_at: datetime | None = None
    if state is not None and state.rendered_files:
        files = [
            RenderedConfigFileEntry(path=f["path"], content=f["content"])
            for f in state.rendered_files
            if isinstance(f, dict) and "path" in f and "content" in f
        ]
        rendered_at = state.rendered_at
    return RenderedConfigResponse(server_id=server.id, rendered_at=rendered_at, files=files)


class RndcStatusResponse(BaseModel):
    server_id: uuid.UUID
    observed_at: datetime | None
    text: str | None


@router.get(
    "/servers/{server_id}/rndc-status",
    response_model=RndcStatusResponse,
)
async def get_server_rndc_status(
    server_id: uuid.UUID, db: DB, _: CurrentUser
) -> RndcStatusResponse:
    """Latest agent-pushed ``rndc status`` output for this server."""
    server = await db.get(DNSServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")
    state = await db.get(DNSServerRuntimeState, server_id)
    if state is None:
        return RndcStatusResponse(server_id=server.id, observed_at=None, text=None)
    return RndcStatusResponse(
        server_id=server.id,
        observed_at=state.rndc_observed_at,
        text=state.rndc_status_text,
    )


@router.put("/groups/{group_id}/zones/{zone_id}", response_model=ZoneResponse)
async def update_zone(
    group_id: uuid.UUID, zone_id: uuid.UUID, body: ZoneUpdate, db: DB, current_user: SuperAdmin
) -> DNSZone:
    zone = await _require_zone(group_id, zone_id, db)
    _reject_if_synthesised_zone(zone, "edit")
    changes = body.model_dump(exclude_none=True)
    # ``color`` is the one field on this schema where NULL is a meaningful
    # user intent ("clear the color"). Re-inject it when explicitly set to
    # None in the incoming payload — exclude_none would otherwise drop it.
    if "color" in body.model_fields_set and body.color is None:
        changes["color"] = None
    for k, v in changes.items():
        setattr(zone, k, v)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=zone.name,
            changed_fields=list(changes.keys()),
            result="success",
        )
    )
    await db.commit()
    await db.refresh(zone)
    return zone


@router.delete("/groups/{group_id}/zones/{zone_id}", status_code=204)
async def delete_zone(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
    permanent: bool = False,
) -> None:
    """Delete a DNS zone.

    Default behavior is soft-delete: the zone + every record in it gets
    stamped with the same ``deletion_batch_id``. Records cascade alongside
    the zone, so a single restore brings them all back atomically. The
    Windows write-through (``apply_zone_change(..., "delete")``) is
    deliberately skipped on the soft-delete path — the zone hasn't actually
    been removed from BIND9 / the agent's bundle yet, so we don't yank the
    serving zone out from under live clients. Permanent delete still pushes
    the write-through first.

    ``?permanent=true`` runs the legacy hard-delete path (super-admin only).
    """
    zone = await _require_zone(group_id, zone_id, db)
    _reject_if_synthesised_zone(zone, "delete")

    if not permanent:
        batch = await collect_soft_delete_batch(db, zone)
        apply_soft_delete(batch, current_user.id)
        for row in batch.rows:
            db.add(
                AuditLog(
                    user_id=current_user.id,
                    user_display_name=current_user.display_name,
                    auth_source=current_user.auth_source,
                    action="soft_delete",
                    resource_type=row.resource_type,
                    resource_id=str(row.obj.id),
                    resource_display=row.display,
                    old_value={"deletion_batch_id": str(batch.batch_id)},
                    result="success",
                )
            )
        await db.commit()
        return

    # Same write-through contract as create: push the delete first, only
    # drop the DB row if the Windows side agreed.
    await _push_zone_to_agentless_servers(db, zone, "delete")
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=zone.name,
            result="success",
        )
    )
    await db.delete(zone)
    await db.commit()


# ── Zone template wizard ────────────────────────────────────────────────────


class FromTemplateRequest(BaseModel):
    template_id: str
    zone_name: str
    params: dict[str, str] = {}
    view_id: uuid.UUID | None = None
    zone_type: str = "primary"
    kind: str = "forward"

    @field_validator("zone_name")
    @classmethod
    def ensure_trailing_dot(cls, v: str) -> str:
        return v if v.endswith(".") else v + "."


@router.get("/zone-templates")
async def list_zone_templates(_: CurrentUser) -> dict[str, Any]:
    """Return the static catalog of zone templates."""
    templates = list_templates()
    return {
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "category": t.category,
                "description": t.description,
                "parameters": [
                    {
                        "key": p.key,
                        "label": p.label,
                        "type": p.type,
                        "required": p.required,
                        "default": p.default,
                        "placeholder": p.placeholder,
                        "hint": p.hint,
                    }
                    for p in t.parameters
                ],
                "record_count": len(t.records),
            }
            for t in templates
        ]
    }


@router.post(
    "/groups/{group_id}/zones/from-template",
    response_model=ZoneResponse,
    status_code=201,
)
async def create_zone_from_template(
    group_id: uuid.UUID, body: FromTemplateRequest, db: DB, current_user: SuperAdmin
) -> DNSZone:
    """Create a zone + materialise the template's records in one transaction."""
    await _require_group(group_id, db)
    template = get_template(body.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Unknown template: {body.template_id}")

    errors = validate_params(template, body.params)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    existing = await db.execute(
        select(DNSZone).where(
            DNSZone.group_id == group_id,
            DNSZone.view_id == body.view_id,
            DNSZone.name == body.zone_name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A zone with that name already exists in this group/view"
        )

    zone = DNSZone(
        group_id=group_id,
        view_id=body.view_id,
        name=body.zone_name,
        zone_type=body.zone_type,
        kind=body.kind,
    )
    db.add(zone)
    await _push_zone_to_agentless_servers(db, zone, "create")
    await db.flush()

    record_payloads = materialize(template, body.zone_name, body.params)
    created_records: list[DNSRecord] = []
    for r in record_payloads:
        if r["record_type"] not in VALID_RECORD_TYPES:
            # Catalog-author bug — refuse rather than write garbage records.
            raise HTTPException(
                status_code=500,
                detail=f"Template {body.template_id} produced invalid record type {r['record_type']}",
            )
        fqdn = (f"{r['name']}.{zone.name}" if r["name"] != "@" else zone.name).rstrip(".") + "."
        rec = DNSRecord(
            zone_id=zone.id,
            fqdn=fqdn,
            name=r["name"],
            record_type=r["record_type"],
            value=r["value"],
            ttl=r["ttl"],
            priority=r["priority"],
            weight=r["weight"],
            port=r["port"],
            created_by_user_id=current_user.id,
        )
        db.add(rec)
        created_records.append(rec)

    target_serial = bump_zone_serial(zone) if record_payloads else None
    await db.flush()
    for rec in created_records:
        await enqueue_record_op(
            db,
            zone,
            "create",
            {
                "name": rec.name,
                "type": rec.record_type,
                "value": rec.value,
                "ttl": rec.ttl,
                "priority": rec.priority,
                "weight": rec.weight,
                "port": rec.port,
            },
            target_serial=target_serial,
        )

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=zone.name,
            new_value={
                "from_template": template.id,
                "records_created": len(created_records),
            },
            result="success",
        )
    )
    await db.commit()
    await db.refresh(zone)
    return zone


# ── Zone delegation wizard ──────────────────────────────────────────────────


@router.get("/groups/{group_id}/zones/{zone_id}/delegation-preview")
async def get_delegation_preview(
    group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, _: CurrentUser
) -> dict[str, Any]:
    """Compute the NS + glue records needed to delegate this zone from its parent.

    Returns ``{has_parent: false}`` when no eligible parent zone exists in
    the same group; otherwise the dict is the full preview shape from
    ``preview_to_dict`` plus ``has_parent: true``.
    """
    child = await _require_zone(group_id, zone_id, db)
    parent = await find_parent_zone(db, group_id, child.name)
    if parent is None:
        return {"has_parent": False}
    preview = await compute_delegation(db, parent, child)
    return {"has_parent": True, **preview_to_dict(preview)}


@router.post(
    "/groups/{group_id}/zones/{zone_id}/delegate-from-parent",
    response_model=list[RecordResponse],
    status_code=201,
)
async def apply_delegation(
    group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, current_user: CurrentUser
) -> list[DNSRecord]:
    """Materialise the delegation records into the parent zone.

    Idempotent — records that already exist in the parent are skipped, so a
    second call after the first succeeds is a no-op. Each created record
    flows through ``enqueue_record_op`` so the parent zone's serial bumps
    once and the agent / Windows-driver push happens uniformly.
    """
    child = await _require_zone(group_id, zone_id, db)
    parent = await find_parent_zone(db, group_id, child.name)
    if parent is None:
        raise HTTPException(
            status_code=404,
            detail=f"No parent zone found in this group for {child.name.rstrip('.')}",
        )
    preview = await compute_delegation(db, parent, child)
    pending = preview.ns_records_to_create + preview.glue_records_to_create
    if not pending:
        return []
    if preview.child_apex_ns_count == 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delegate {child.name.rstrip('.')} — it has no NS records at "
                "the apex. Add at least one NS record before delegating."
            ),
        )

    created: list[DNSRecord] = []
    for r in pending:
        fqdn = (f"{r.name}.{parent.name}" if r.name != "@" else parent.name).rstrip(".") + "."
        record = DNSRecord(
            zone_id=parent.id,
            fqdn=fqdn,
            name=r.name,
            record_type=r.record_type,
            value=r.value,
            ttl=r.ttl,
            created_by_user_id=current_user.id,
        )
        db.add(record)
        created.append(record)

    target_serial = bump_zone_serial(parent)
    await db.flush()
    for record in created:
        await enqueue_record_op(
            db,
            parent,
            "create",
            {
                "name": record.name,
                "type": record.record_type,
                "value": record.value,
                "ttl": record.ttl,
                "priority": record.priority,
                "weight": record.weight,
                "port": record.port,
            },
            target_serial=target_serial,
        )

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delegate",
            resource_type="dns_zone",
            resource_id=str(parent.id),
            resource_display=parent.name,
            new_value={
                "child_zone": child.name,
                "ns_created": len(preview.ns_records_to_create),
                "glue_created": len(preview.glue_records_to_create),
            },
            result="success",
        )
    )
    await db.commit()
    for record in created:
        await db.refresh(record)
    return created


async def _push_zone_to_agentless_servers(db: DB, zone: DNSZone, op: str) -> None:
    """Push ``create`` / ``delete`` to every agentless-with-creds server.

    "Agentless" today means ``windows_dns``; "with creds" means Path B
    (WinRM). Those are the only servers that have an admin channel the
    control plane can use directly — agent-based drivers (bind9) get
    zone changes through the ConfigBundle long-poll, not here.

    Failure surfaces as a 502 so the caller's ``db.commit()`` never runs
    — the DB row stays in an uncommitted state and the session rollback
    cleans it up. Matches the DHCP write-through pattern.
    """
    from app.drivers.dns import get_driver, is_agentless  # noqa: PLC0415

    servers_res = await db.execute(
        select(DNSServer).where(
            DNSServer.group_id == zone.group_id,
            DNSServer.credentials_encrypted.isnot(None),
        )
    )
    targets = [s for s in servers_res.scalars().all() if is_agentless(s.driver)]
    if not targets:
        return

    errors: list[str] = []
    for server in targets:
        driver = get_driver(server.driver)
        if not hasattr(driver, "apply_zone_change"):
            continue
        try:
            await driver.apply_zone_change(server, zone, op)
        except Exception as exc:  # noqa: BLE001 — surface error verbatim to user
            errors.append(f"{server.name}: {exc}")
            logger.warning(
                "dns.zone.push_agentless_failed",
                server=str(server.id),
                zone=zone.name,
                op=op,
                error=str(exc),
            )

    if errors:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to {op} zone on Windows DNS: {'; '.join(errors)}. "
                "Zone state in SpatiumDDI was not changed."
            ),
        )


# ── Record endpoints ────────────────────────────────────────────────────────


class GroupRecordResponse(BaseModel):
    """Record list item for the group-wide Records tab.

    Includes zone + view name context so the UI doesn't have to join, and
    all DNSRecord fields so the existing RecordModal can edit in place.
    """

    id: uuid.UUID
    zone_id: uuid.UUID
    zone_name: str
    view_id: uuid.UUID | None
    view_name: str | None
    name: str
    fqdn: str
    record_type: str
    value: str
    ttl: int | None
    priority: int | None
    weight: int | None
    port: int | None
    auto_generated: bool
    tailscale_tenant_id: uuid.UUID | None = None
    pool_member_id: uuid.UUID | None = None
    created_at: datetime
    modified_at: datetime


@router.get(
    "/groups/{group_id}/records",
    response_model=list[GroupRecordResponse],
)
async def list_group_records(
    group_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[GroupRecordResponse]:
    """Every record across every zone in the group, with zone + view context."""
    await _require_group(group_id, db)

    zones = list(
        (await db.execute(select(DNSZone).where(DNSZone.group_id == group_id))).scalars().all()
    )
    if not zones:
        return []
    zone_by_id = {z.id: z for z in zones}

    views = list(
        (await db.execute(select(DNSView).where(DNSView.group_id == group_id))).scalars().all()
    )
    view_name_by_id = {v.id: v.name for v in views}

    records = list(
        (
            await db.execute(
                select(DNSRecord)
                .where(DNSRecord.zone_id.in_(list(zone_by_id.keys())))
                .order_by(DNSRecord.fqdn, DNSRecord.record_type)
            )
        )
        .scalars()
        .all()
    )
    out: list[GroupRecordResponse] = []
    for rec in records:
        zone = zone_by_id.get(rec.zone_id)
        if zone is None:
            continue
        out.append(
            GroupRecordResponse(
                id=rec.id,
                zone_id=zone.id,
                zone_name=zone.name.rstrip("."),
                view_id=rec.view_id,
                view_name=(view_name_by_id.get(rec.view_id) if rec.view_id else None),
                name=rec.name,
                fqdn=rec.fqdn,
                record_type=rec.record_type,
                value=rec.value,
                ttl=rec.ttl,
                priority=rec.priority,
                weight=rec.weight,
                port=rec.port,
                auto_generated=rec.auto_generated,
                tailscale_tenant_id=rec.tailscale_tenant_id,
                pool_member_id=rec.pool_member_id,
                created_at=rec.created_at,
                modified_at=rec.modified_at,
            )
        )
    return out


@router.get("/groups/{group_id}/zones/{zone_id}/records", response_model=list[RecordResponse])
async def list_records(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    tag: list[str] = Query(default_factory=list),
) -> list[DNSRecord]:
    await _require_zone(group_id, zone_id, db)
    stmt = (
        select(DNSRecord)
        .where(DNSRecord.zone_id == zone_id)
        .order_by(DNSRecord.name, DNSRecord.record_type)
    )
    stmt = apply_tag_filter(stmt, DNSRecord.tags, tag)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post(
    "/groups/{group_id}/zones/{zone_id}/records", response_model=RecordResponse, status_code=201
)
async def create_record(
    group_id: uuid.UUID, zone_id: uuid.UUID, body: RecordCreate, db: DB, current_user: CurrentUser
) -> DNSRecord:
    zone = await _require_zone(group_id, zone_id, db)
    _reject_if_synthesised_zone(zone, "add records to")
    fqdn = f"{body.name}.{zone.name}" if body.name != "@" else zone.name

    record = DNSRecord(
        zone_id=zone_id,
        fqdn=fqdn,
        created_by_user_id=current_user.id,
        **body.model_dump(),
    )
    db.add(record)
    target_serial = bump_zone_serial(zone)
    await db.flush()
    await enqueue_record_op(
        db,
        zone,
        "create",
        {
            "name": record.name,
            "type": record.record_type,
            "value": record.value,
            "ttl": record.ttl,
            "priority": record.priority,
            "weight": record.weight,
            "port": record.port,
        },
        target_serial=target_serial,
    )
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="dns_record",
            resource_id=str(record.id),
            resource_display=fqdn,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(record)
    return record


@router.put("/groups/{group_id}/zones/{zone_id}/records/{record_id}", response_model=RecordResponse)
async def update_record(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    record_id: uuid.UUID,
    body: RecordUpdate,
    db: DB,
    current_user: CurrentUser,
) -> DNSRecord:
    record = await _require_record(group_id, zone_id, record_id, db)
    _reject_if_synthesised_record(record, "edit")
    zone = await db.get(DNSZone, record.zone_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(record, k, v)
    if "name" in changes and zone:
        record.fqdn = f"{record.name}.{zone.name}" if record.name != "@" else zone.name
    target_serial = bump_zone_serial(zone) if zone is not None else None
    if zone is not None:
        await enqueue_record_op(
            db,
            zone,
            "update",
            {
                "name": record.name,
                "type": record.record_type,
                "value": record.value,
                "ttl": record.ttl,
                "priority": record.priority,
                "weight": record.weight,
                "port": record.port,
            },
            target_serial=target_serial,
        )

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="dns_record",
            resource_id=str(record.id),
            resource_display=record.fqdn,
            changed_fields=list(changes.keys()),
            result="success",
        )
    )
    await db.commit()
    await db.refresh(record)
    return record


@router.delete("/groups/{group_id}/zones/{zone_id}/records/{record_id}", status_code=204)
async def delete_record(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    record_id: uuid.UUID,
    db: DB,
    current_user: CurrentUser,
    permanent: bool = False,
) -> None:
    """Delete a DNS record.

    Default soft-delete stamps the record with a fresh ``deletion_batch_id``
    so it can be individually restored from /admin/trash. The DDNS / agent
    record-op is enqueued only on the permanent path; on soft-delete the
    record is hidden from queries but still in the served bundle until the
    next render — operators who want to re-instate it within the trash
    window won't see a serial bump round trip first.
    """
    record = await _require_record(group_id, zone_id, record_id, db)
    _reject_if_synthesised_record(record, "delete")

    if not permanent:
        batch = await collect_soft_delete_batch(db, record)
        apply_soft_delete(batch, current_user.id)
        for row in batch.rows:
            db.add(
                AuditLog(
                    user_id=current_user.id,
                    user_display_name=current_user.display_name,
                    auth_source=current_user.auth_source,
                    action="soft_delete",
                    resource_type=row.resource_type,
                    resource_id=str(row.obj.id),
                    resource_display=row.display,
                    old_value={"deletion_batch_id": str(batch.batch_id)},
                    result="success",
                )
            )
        await db.commit()
        return

    from app.api.deps import require_superadmin  # noqa: PLC0415

    require_superadmin(current_user)

    zone = await db.get(DNSZone, record.zone_id)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="dns_record",
            resource_id=str(record.id),
            resource_display=record.fqdn,
            result="success",
        )
    )
    rec_snapshot = {
        "name": record.name,
        "type": record.record_type,
        "value": record.value,
        "ttl": record.ttl,
        "priority": record.priority,
        "weight": record.weight,
        "port": record.port,
    }
    await db.delete(record)
    if zone is not None:
        target_serial = bump_zone_serial(zone)
        await enqueue_record_op(db, zone, "delete", rec_snapshot, target_serial=target_serial)
    await db.commit()


class BulkDeleteRecordsRequest(BaseModel):
    """IDs of auto- or manually-created records to delete in one shot.

    All IDs must belong to the zone in the URL — cross-zone deletion
    isn't allowed here (the UI scopes bulk ops to a single zone). Any
    record ID that doesn't belong to the zone is skipped with a reason
    so a partial payload doesn't fail the whole batch.
    """

    record_ids: list[uuid.UUID]


class BulkDeleteRecordsResponse(BaseModel):
    deleted: int
    skipped: list[dict[str, str]]  # each: {record_id, reason}


@router.post(
    "/groups/{group_id}/zones/{zone_id}/records/bulk-delete",
    response_model=BulkDeleteRecordsResponse,
)
async def bulk_delete_records(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    body: BulkDeleteRecordsRequest,
    db: DB,
    current_user: CurrentUser,
) -> BulkDeleteRecordsResponse:
    """Delete many records from one zone in a single transaction.

    Avoids the N-round-trip cost of the UI fanning out N singular
    DELETEs — groups every op into one ``enqueue_record_ops_batch`` call
    so agentless Windows DNS sees one WinRM round trip for the whole
    batch instead of one per record. BIND9 still writes N queued rows
    (the agent will batch-ship on its next poll).
    """
    if not body.record_ids:
        return BulkDeleteRecordsResponse(deleted=0, skipped=[])

    zone = await _require_zone(group_id, zone_id, db)

    res = await db.execute(select(DNSRecord).where(DNSRecord.id.in_(body.record_ids)))
    records = list(res.scalars().all())
    by_id = {r.id: r for r in records}

    skipped: list[dict[str, str]] = []
    targets: list[DNSRecord] = []
    for rid in body.record_ids:
        rec = by_id.get(rid)
        if rec is None:
            skipped.append({"record_id": str(rid), "reason": "not found"})
            continue
        if rec.zone_id != zone_id:
            skipped.append({"record_id": str(rid), "reason": "wrong zone"})
            continue
        targets.append(rec)

    if not targets:
        return BulkDeleteRecordsResponse(deleted=0, skipped=skipped)

    # Bump the zone serial once for the whole batch — same zone, same
    # serial bump. N singular deletes used to bump N times, which is
    # wasteful and produces confusing serial jumps in SOA queries.
    target_serial = bump_zone_serial(zone)

    ops = [
        {
            "op": "delete",
            "record": {
                "name": r.name,
                "type": r.record_type,
                "value": r.value,
                "ttl": r.ttl,
                "priority": r.priority,
                "weight": r.weight,
                "port": r.port,
            },
            "target_serial": target_serial,
        }
        for r in targets
    ]

    # One driver round trip per chunk for agentless; N queued rows for
    # agent-based (the agent flushes them on its next long-poll).
    try:
        op_rows = await enqueue_record_ops_batch(db, zone, ops)
    except Exception as exc:  # noqa: BLE001 — whole-batch wire / auth failure
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bulk delete dispatch failed on zone {zone.name}: {exc}",
        ) from exc

    # Per-op state determines the DB delete. A wire delete that came back
    # failed must NOT remove the DB row — if we delete blindly the user
    # sees "deleted" in the UI but the record is still published, and
    # the next "Sync with server" pulls the zombie back.
    deleted = 0
    for rec, op_row in zip(targets, op_rows, strict=True):
        if op_row is None:
            skipped.append(
                {
                    "record_id": str(rec.id),
                    "reason": "no primary configured for zone — wire delete skipped",
                }
            )
            continue
        if op_row.state != "applied":
            skipped.append(
                {
                    "record_id": str(rec.id),
                    "reason": f"wire delete failed: {op_row.last_error or 'unknown'}",
                }
            )
            continue
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="delete",
                resource_type="dns_record",
                resource_id=str(rec.id),
                resource_display=rec.fqdn,
                result="success",
            )
        )
        await db.delete(rec)
        deleted += 1

    await db.commit()
    return BulkDeleteRecordsResponse(deleted=deleted, skipped=skipped)


# ── Bulk zone import / export ───────────────────────────────────────────────
# Zone-file parsing and rendering live in app.services.dns_io so this router
# stays thin (CLAUDE.md non-negotiable #10: driver / service logic does not
# leak into the API layer).


VALID_CONFLICT_STRATEGIES = {"merge", "replace", "append"}


class ImportPreviewRequest(BaseModel):
    """Zone-file import preview payload.

    ``zone_name`` is used as the $ORIGIN if the zone file does not set one.
    Either ``zone_id`` (import into existing zone) or (``group_id`` + zone_name
    for a zone that does not exist yet) must be resolvable from the URL path.
    """

    zone_file: str
    zone_name: str | None = None
    view_id: uuid.UUID | None = None


class ImportCommitRequest(ImportPreviewRequest):
    conflict_strategy: str = "merge"

    @field_validator("conflict_strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        if v not in VALID_CONFLICT_STRATEGIES:
            raise ValueError(
                f"conflict_strategy must be one of {sorted(VALID_CONFLICT_STRATEGIES)}"
            )
        return v


class RecordChangeResponse(BaseModel):
    op: str
    name: str
    record_type: str
    value: str
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None
    existing_id: str | None = None


class ImportPreviewResponse(BaseModel):
    zone_id: uuid.UUID | None
    zone_name: str
    to_create: list[RecordChangeResponse]
    to_update: list[RecordChangeResponse]
    to_delete: list[RecordChangeResponse]
    unchanged: list[RecordChangeResponse]
    soa_detected: bool
    record_count: int


class ImportCommitResponse(BaseModel):
    zone_id: uuid.UUID
    batch_id: uuid.UUID
    created: int
    updated: int
    deleted: int
    unchanged: int
    conflict_strategy: str


def _resolve_zone_name(body: ImportPreviewRequest, existing_zone: DNSZone | None) -> str:
    if existing_zone is not None:
        return existing_zone.name
    if not body.zone_name:
        raise HTTPException(
            status_code=422,
            detail="zone_name is required when importing into a zone that does not exist yet",
        )
    return body.zone_name if body.zone_name.endswith(".") else body.zone_name + "."


async def _load_zone_records(zone_id: uuid.UUID, db: DB) -> list[DNSRecord]:
    result = await db.execute(select(DNSRecord).where(DNSRecord.zone_id == zone_id))
    return list(result.scalars().all())


def _change_to_response(c: RecordChange) -> RecordChangeResponse:
    return RecordChangeResponse(
        op=c.op,
        name=c.name,
        record_type=c.record_type,
        value=c.value,
        ttl=c.ttl,
        priority=c.priority,
        weight=c.weight,
        port=c.port,
        existing_id=c.existing_id,
    )


@router.post(
    "/groups/{group_id}/zones/{zone_id}/import/preview",
    response_model=ImportPreviewResponse,
)
async def import_zone_preview(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    body: ImportPreviewRequest,
    db: DB,
    _: CurrentUser,
) -> ImportPreviewResponse:
    """Parse a zone file and return the diff against an existing zone.

    Non-mutating: this endpoint never writes to the database.
    """
    zone = await _require_zone(group_id, zone_id, db)
    zone_name = _resolve_zone_name(body, zone)

    try:
        parsed = parse_zone_file(body.zone_file, zone_name)
    except ZoneParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    existing = await _load_zone_records(zone_id, db)
    diff = diff_records(parsed.records, existing)

    return ImportPreviewResponse(
        zone_id=zone.id,
        zone_name=zone.name,
        to_create=[_change_to_response(c) for c in diff.to_create],
        to_update=[_change_to_response(c) for c in diff.to_update],
        to_delete=[_change_to_response(c) for c in diff.to_delete],
        unchanged=[_change_to_response(c) for c in diff.unchanged],
        soa_detected=parsed.soa is not None,
        record_count=len(parsed.records),
    )


@router.post(
    "/groups/{group_id}/zones/{zone_id}/import/commit",
    response_model=ImportCommitResponse,
)
async def import_zone_commit(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    body: ImportCommitRequest,
    db: DB,
    current_user: SuperAdmin,
) -> ImportCommitResponse:
    """Apply a parsed zone file to the existing zone in a single transaction.

    ``conflict_strategy`` controls how conflicts are resolved:

    * ``merge``   — create new, update changed, keep records that are absent
                    from the zone file (additive)
    * ``replace`` — create new, update changed, delete records absent from the
                    zone file (make the zone match the file exactly)
    * ``append``  — only create new records; existing records are left alone

    Auditing: one summary ``AuditLog`` entry is written under
    ``resource_type='dns_zone_import'`` tagged with a ``batch_id``.
    Per-record changes are encoded in the ``new_value`` JSONB payload so
    per-record history is recoverable without generating N audit rows.
    """
    zone = await _require_zone(group_id, zone_id, db)
    zone_name = _resolve_zone_name(body, zone)

    try:
        parsed = parse_zone_file(body.zone_file, zone_name)
    except ZoneParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    existing = await _load_zone_records(zone_id, db)
    diff = diff_records(parsed.records, existing)

    batch_id = uuid.uuid4()
    created = 0
    updated = 0
    deleted = 0
    unchanged_count = len(diff.unchanged)

    existing_by_id: dict[str, DNSRecord] = {str(r.id): r for r in existing}

    # Creates run under merge, replace, and append.
    for change in diff.to_create:
        fqdn = f"{change.name}.{zone.name}" if change.name != "@" else zone.name
        db.add(
            DNSRecord(
                zone_id=zone.id,
                name=change.name,
                fqdn=fqdn,
                record_type=change.record_type,
                value=change.value,
                ttl=change.ttl,
                priority=change.priority,
                weight=change.weight,
                port=change.port,
                created_by_user_id=current_user.id,
            )
        )
        created += 1

    # Updates only under merge + replace.
    if body.conflict_strategy in {"merge", "replace"}:
        for change in diff.to_update:
            row = existing_by_id.get(change.existing_id or "")
            if row is None:
                continue
            row.ttl = change.ttl
            row.priority = change.priority
            row.weight = change.weight
            row.port = change.port
            updated += 1

    # Deletes only under replace.
    if body.conflict_strategy == "replace":
        for change in diff.to_delete:
            row = existing_by_id.get(change.existing_id or "")
            if row is None:
                continue
            await db.delete(row)
            deleted += 1

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="import",
            resource_type="dns_zone_import",
            resource_id=str(zone.id),
            resource_display=zone.name,
            new_value={
                "batch_id": str(batch_id),
                "conflict_strategy": body.conflict_strategy,
                "created": created,
                "updated": updated,
                "deleted": deleted,
                "unchanged": unchanged_count,
                "changes": {
                    "create": [
                        {"name": c.name, "type": c.record_type, "value": c.value}
                        for c in diff.to_create
                    ],
                    "update": (
                        [
                            {"name": c.name, "type": c.record_type, "value": c.value}
                            for c in diff.to_update
                        ]
                        if body.conflict_strategy in {"merge", "replace"}
                        else []
                    ),
                    "delete": (
                        [
                            {"name": c.name, "type": c.record_type, "value": c.value}
                            for c in diff.to_delete
                        ]
                        if body.conflict_strategy == "replace"
                        else []
                    ),
                },
            },
            result="success",
        )
    )

    await db.commit()

    logger.info(
        "dns_zone_import",
        batch_id=str(batch_id),
        zone_id=str(zone.id),
        zone_name=zone.name,
        conflict_strategy=body.conflict_strategy,
        created=created,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged_count,
    )

    return ImportCommitResponse(
        zone_id=zone.id,
        batch_id=batch_id,
        created=created,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged_count,
        conflict_strategy=body.conflict_strategy,
    )


class SyncWithServerRequest(BaseModel):
    """Body for the zone's ``sync-with-server`` action. ``apply=False`` runs
    preview-only (neither the DB nor the authoritative server is touched)."""

    apply: bool = True


class SyncWithServerResponse(BaseModel):
    # Pull (server → DB)
    server_records: int
    existing_in_db: int
    imported: int
    skipped_unsupported: int
    imported_records: list[dict]
    # Push (DB → server)
    push_candidates: int
    pushed: int
    pushed_records: list[dict]
    push_errors: list[str]


@router.post(
    "/groups/{group_id}/zones/{zone_id}/sync-with-server",
    response_model=SyncWithServerResponse,
)
async def sync_zone_with_server_endpoint(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    body: SyncWithServerRequest,
    db: DB,
    current_user: CurrentUser,
) -> SyncWithServerResponse:
    """Bi-directional additive sync between SpatiumDDI's DB and the zone's
    primary authoritative server.

    Phase 1 (pull): AXFR the server, create DB rows for anything present
    on the wire but missing from our DB.
    Phase 2 (push): for every DB row that isn't on the wire, send an RFC
    2136 add so it lands on the server.

    Never deletes on either side. Destructive reconciliation is a future
    iteration.
    """
    from app.services.dns.pull_from_server import sync_zone_with_server

    zone = await _require_zone(group_id, zone_id, db)
    try:
        result = await sync_zone_with_server(db, zone, apply=body.apply)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(
            "dns.sync_with_server_failed",
            zone=zone.name,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Sync with authoritative server failed: {exc}. "
                "Check that the server allows zone transfers and dynamic "
                "updates from this host."
            ),
        ) from exc

    if body.apply and (result.pull.imported or result.push.pushed or result.push.push_errors):
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="sync_with_server",
                resource_type="dns_zone",
                resource_id=str(zone.id),
                resource_display=zone.name,
                result="error" if result.push.push_errors else "success",
                new_value={
                    "imported": result.pull.imported,
                    "pushed": result.push.pushed,
                    "push_errors": len(result.push.push_errors),
                    "server_records": result.pull.server_records,
                },
            )
        )
        await db.commit()
    return SyncWithServerResponse(
        server_records=result.pull.server_records,
        existing_in_db=result.pull.existing_in_db,
        imported=result.pull.imported,
        skipped_unsupported=result.pull.skipped_unsupported,
        imported_records=result.pull.imported_records,
        push_candidates=result.push.candidates,
        pushed=result.push.pushed,
        pushed_records=result.push.pushed_records,
        push_errors=result.push.push_errors,
    )


@router.get("/groups/{group_id}/zones/{zone_id}/export")
async def export_zone(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
) -> Response:
    """Return the zone as an RFC 1035 zone file."""
    zone = await _require_zone(group_id, zone_id, db)
    records = await _load_zone_records(zone_id, db)
    text = write_zone_file(zone, records)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"{zone.name.rstrip('.')}-{ts}.zone"
    return Response(
        content=text,
        media_type="text/dns",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Helper functions ────────────────────────────────────────────────────────


async def _require_group(group_id: uuid.UUID, db: DB) -> DNSServerGroup:
    group = await db.get(DNSServerGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Server group not found")
    return group


async def _require_server(group_id: uuid.UUID, server_id: uuid.UUID, db: DB) -> DNSServer:
    result = await db.execute(
        select(DNSServer).where(DNSServer.id == server_id, DNSServer.group_id == group_id)
    )
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


async def _require_acl(group_id: uuid.UUID, acl_id: uuid.UUID, db: DB) -> DNSAcl:
    acl = await _load_acl(group_id, acl_id, db)
    if not acl:
        raise HTTPException(status_code=404, detail="ACL not found")
    return acl


async def _require_view(group_id: uuid.UUID, view_id: uuid.UUID, db: DB) -> DNSView:
    result = await db.execute(
        select(DNSView).where(DNSView.id == view_id, DNSView.group_id == group_id)
    )
    view = result.scalar_one_or_none()
    if not view:
        raise HTTPException(status_code=404, detail="View not found")
    return view


async def _require_zone(group_id: uuid.UUID, zone_id: uuid.UUID, db: DB) -> DNSZone:
    result = await db.execute(
        select(DNSZone).where(DNSZone.id == zone_id, DNSZone.group_id == group_id)
    )
    zone = result.scalar_one_or_none()
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    return zone


async def _require_record(
    group_id: uuid.UUID, zone_id: uuid.UUID, record_id: uuid.UUID, db: DB
) -> DNSRecord:
    # Verify zone belongs to group first
    await _require_zone(group_id, zone_id, db)
    result = await db.execute(
        select(DNSRecord).where(DNSRecord.id == record_id, DNSRecord.zone_id == zone_id)
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


def _reject_if_synthesised_zone(zone: DNSZone, op: str) -> None:
    """Refuse writes against a Tailscale-synthesised zone.

    Phase 2 materialises ``<tailnet>.ts.net`` from the device list
    and the reconciler is the only authorised writer — operator
    edits would be silently overwritten on the next sync, so we
    block them at the API instead. To make changes, delete the
    Tailscale tenant (or rebind it to a different DNS group), then
    the operator can manage the zone manually.
    """
    if zone.tailscale_tenant_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Zone {zone.name!r} is synthesised by the Tailscale "
                f"integration and cannot be {op}ed manually. The reconciler "
                f"will overwrite any changes on the next sync. Unbind the "
                f"DNS group on the Tailscale tenant or delete the tenant "
                f"to release the zone."
            ),
        )


def _reject_if_synthesised_record(record: DNSRecord, op: str) -> None:
    """Same gate, applied to records belonging to a synthesised zone."""
    if record.tailscale_tenant_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Record {record.fqdn!r} is synthesised by the Tailscale "
                f"integration and cannot be {op}ed manually. Edits would be "
                f"overwritten on the next sync."
            ),
        )
    if record.pool_member_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Record {record.fqdn!r} is managed by a DNS pool and cannot "
                f"be {op}ed manually. Edits would be overwritten on the next "
                f"health-check pass — manage the pool / member instead."
            ),
        )
