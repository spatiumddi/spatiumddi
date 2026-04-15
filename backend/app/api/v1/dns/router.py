"""DNS API: server groups, servers, server options, views, ACLs, zones, records."""

import io
import uuid
import zipfile
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.models.audit import AuditLog
from app.models.dns import (
    DNSAcl,
    DNSAclEntry,
    DNSRecord,
    DNSServer,
    DNSServerGroup,
    DNSServerOptions,
    DNSTrustAnchor,
    DNSView,
    DNSZone,
)
from app.services.dns.record_ops import enqueue_record_op
from app.services.dns.serial import bump_zone_serial
from app.services.dns_io import (
    RecordChange,
    ZoneParseError,
    diff_records,
    parse_zone_file,
    write_zone_file,
)

logger = structlog.get_logger(__name__)
router = APIRouter()

VALID_DRIVERS = {"bind9"}
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
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


# ── Server schemas ──────────────────────────────────────────────────────────


class ServerCreate(BaseModel):
    name: str
    driver: str = "bind9"
    host: str
    port: int = 53
    api_port: int | None = None
    api_key: str | None = None
    roles: list[str] = []
    notes: str = ""

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
    last_sync_at: datetime | None
    last_health_check_at: datetime | None
    notes: str
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


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
    linked_subnet_id: uuid.UUID | None = None
    allow_query: list[str] | None = None
    allow_transfer: list[str] | None = None
    also_notify: list[str] | None = None
    notify_enabled: str | None = None

    @field_validator("zone_type")
    @classmethod
    def validate_zone_type(cls, v: str) -> str:
        if v not in VALID_ZONE_TYPES:
            raise ValueError(f"zone_type must be one of {sorted(VALID_ZONE_TYPES)}")
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
    linked_subnet_id: uuid.UUID | None = None
    allow_query: list[str] | None = None
    allow_transfer: list[str] | None = None
    also_notify: list[str] | None = None
    notify_enabled: str | None = None

    @field_validator("zone_type")
    @classmethod
    def validate_zone_type(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_ZONE_TYPES:
            raise ValueError(f"zone_type must be one of {sorted(VALID_ZONE_TYPES)}")
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
    dnssec_enabled: bool
    last_serial: int
    last_pushed_at: datetime | None
    allow_query: list[str] | None
    allow_transfer: list[str] | None
    also_notify: list[str] | None
    notify_enabled: str | None
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
async def list_servers(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[DNSServer]:
    await _require_group(group_id, db)
    result = await db.execute(
        select(DNSServer).where(DNSServer.group_id == group_id).order_by(DNSServer.name)
    )
    return list(result.scalars().all())


@router.post(
    "/groups/{group_id}/servers", response_model=ServerResponse, status_code=status.HTTP_201_CREATED
)
async def create_server(
    group_id: uuid.UUID, body: ServerCreate, db: DB, current_user: SuperAdmin
) -> DNSServer:
    await _require_group(group_id, db)
    existing = await db.execute(
        select(DNSServer).where(DNSServer.group_id == group_id, DNSServer.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A server with that name already exists in this group"
        )

    data = body.model_dump(exclude={"api_key"})
    data["group_id"] = group_id
    if body.api_key:
        # TODO: encrypt before storing
        data["api_key_encrypted"] = body.api_key

    server = DNSServer(**data)
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
    return server


@router.put("/groups/{group_id}/servers/{server_id}", response_model=ServerResponse)
async def update_server(
    group_id: uuid.UUID, server_id: uuid.UUID, body: ServerUpdate, db: DB, current_user: SuperAdmin
) -> DNSServer:
    server = await _require_server(group_id, server_id, db)
    changes = body.model_dump(exclude_none=True, exclude={"api_key"})
    if body.api_key is not None:
        changes["api_key_encrypted"] = body.api_key  # TODO: encrypt
    for k, v in changes.items():
        setattr(server, k, v)
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
    return server


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
async def list_zones(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[DNSZone]:
    await _require_group(group_id, db)
    result = await db.execute(
        select(DNSZone).where(DNSZone.group_id == group_id).order_by(DNSZone.name)
    )
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


@router.get("/groups/{group_id}/zones/{zone_id}", response_model=ZoneResponse)
async def get_zone(group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, _: CurrentUser) -> DNSZone:
    return await _require_zone(group_id, zone_id, db)


@router.put("/groups/{group_id}/zones/{zone_id}", response_model=ZoneResponse)
async def update_zone(
    group_id: uuid.UUID, zone_id: uuid.UUID, body: ZoneUpdate, db: DB, current_user: SuperAdmin
) -> DNSZone:
    zone = await _require_zone(group_id, zone_id, db)
    changes = body.model_dump(exclude_none=True)
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
    group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> None:
    zone = await _require_zone(group_id, zone_id, db)
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


# ── Record endpoints ────────────────────────────────────────────────────────


@router.get("/groups/{group_id}/zones/{zone_id}/records", response_model=list[RecordResponse])
async def list_records(
    group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[DNSRecord]:
    await _require_zone(group_id, zone_id, db)
    result = await db.execute(
        select(DNSRecord)
        .where(DNSRecord.zone_id == zone_id)
        .order_by(DNSRecord.name, DNSRecord.record_type)
    )
    return list(result.scalars().all())


@router.post(
    "/groups/{group_id}/zones/{zone_id}/records", response_model=RecordResponse, status_code=201
)
async def create_record(
    group_id: uuid.UUID, zone_id: uuid.UUID, body: RecordCreate, db: DB, current_user: CurrentUser
) -> DNSRecord:
    zone = await _require_zone(group_id, zone_id, db)
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
        {"name": record.name, "type": record.record_type, "value": record.value, "ttl": record.ttl},
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
    group_id: uuid.UUID, zone_id: uuid.UUID, record_id: uuid.UUID, db: DB, current_user: CurrentUser
) -> None:
    record = await _require_record(group_id, zone_id, record_id, db)
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
    }
    await db.delete(record)
    if zone is not None:
        target_serial = bump_zone_serial(zone)
        await enqueue_record_op(db, zone, "delete", rec_snapshot, target_serial=target_serial)
    await db.commit()


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


@router.get("/groups/{group_id}/zones/export")
async def export_all_zones(
    group_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    view_id: uuid.UUID | None = None,
) -> StreamingResponse:
    """Return all zones in a group (optionally filtered by view) as a zip."""
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

    filename = f"dns-zones-{group_id}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    filename = zone.name.rstrip(".") + ".zone"
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
