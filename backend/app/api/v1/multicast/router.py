"""Multicast group CRUD — issue #126 Phase 1.

Endpoints under ``/multicast``:

* ``/groups`` — group create / list / read / update / delete
* ``/groups/{id}/ports`` — port range CRUD on a group
* ``/groups/{id}/memberships`` — producer/consumer/RP rows
* ``/memberships/{id}`` — direct delete (the membership id is
  operator-visible in lists, so a flat delete URL avoids forcing
  a round-trip to look up the group_id)
* ``/groups/bulk-allocate/{preview,commit}`` — stamp N sequential
  multicast addresses with a name template in one shot. Bounded
  to 256 per call (the registry is curated; large fan-outs that
  exceed the cap are an operator-error signal, not a workflow).

Permissions: every endpoint is gated on ``multicast`` (admin via
the seeded Network Editor builtin role; superadmin always passes).
Each mutation writes an ``audit_log`` row before commit per
CLAUDE.md non-negotiable #4.

Server-side validation in this layer:

* The ``address`` must parse as an IP and live inside ``224.0.0.0/4``
  IPv4 or ``ff00::/8`` IPv6. The DB CHECK constraint enforces the
  same — Pydantic catches it earlier with a clean 422.
* ``port_end >= port_start`` when ``port_end`` is supplied.
* Membership ``role`` and ``seen_via`` validated against the
  frozensets in the model module.

Bulk-allocate uses the same ``{n}`` / ``{n:03d}`` / ``{n:x}`` /
``{oct1-4}`` template grammar as the IPAM bulk-allocate endpoint
(``app.api.v1.ipam.router``); helpers are imported there to keep
the two surfaces in lock-step.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentUser
from app.api.v1.ownership._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.ipam import IPAddress, IPSpace
from app.models.multicast import (
    MEMBERSHIP_ROLES,
    MEMBERSHIP_SOURCES,
    PORT_TRANSPORTS,
    MulticastGroup,
    MulticastGroupPort,
    MulticastMembership,
)
from app.services.tags import apply_tag_filter

router = APIRouter(
    tags=["multicast"],
    dependencies=[Depends(require_resource_permission("multicast"))],
)

MembershipRole = Literal["producer", "consumer", "rendezvous_point"]
MembershipSource = Literal["manual", "igmp_snooping", "sap_announce"]
PortTransport = Literal["udp", "rtp", "tcp", "srt"]


# IANA-blessed multicast ranges. The DB CHECK constraint mirrors
# this; the Pydantic validator surfaces a clean 422 instead of a
# 500 on the ``IntegrityError``.
_IPV4_MULTICAST = ipaddress.ip_network("224.0.0.0/4")
_IPV6_MULTICAST = ipaddress.ip_network("ff00::/8")


def _validate_multicast_addr(value: str) -> str:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"address must be a valid IP literal: {exc}") from exc
    if isinstance(addr, ipaddress.IPv4Address) and addr in _IPV4_MULTICAST:
        return str(addr)
    if isinstance(addr, ipaddress.IPv6Address) and addr in _IPV6_MULTICAST:
        return str(addr)
    raise ValueError("address must be inside 224.0.0.0/4 (IPv4) or ff00::/8 (IPv6)")


# ── Schemas ─────────────────────────────────────────────────────────


class MulticastGroupPortCreate(BaseModel):
    port_start: int = Field(..., ge=0, le=65535)
    port_end: int | None = Field(default=None, ge=0, le=65535)
    transport: PortTransport = "udp"
    notes: str = ""

    @field_validator("transport")
    @classmethod
    def _v_transport(cls, v: str) -> str:
        if v not in PORT_TRANSPORTS:
            raise ValueError(f"transport must be one of {sorted(PORT_TRANSPORTS)}")
        return v

    @field_validator("port_end")
    @classmethod
    def _v_port_end(cls, v: int | None, info: Any) -> int | None:
        if v is None:
            return None
        start = info.data.get("port_start")
        if start is not None and v < start:
            raise ValueError("port_end must be >= port_start")
        return v


class MulticastGroupPortRead(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    port_start: int
    port_end: int | None
    transport: str
    notes: str

    model_config = {"from_attributes": True}


class MulticastMembershipCreate(BaseModel):
    ip_address_id: uuid.UUID
    role: MembershipRole = "consumer"
    seen_via: MembershipSource = "manual"
    notes: str = ""

    @field_validator("role")
    @classmethod
    def _v_role(cls, v: str) -> str:
        if v not in MEMBERSHIP_ROLES:
            raise ValueError(f"role must be one of {sorted(MEMBERSHIP_ROLES)}")
        return v

    @field_validator("seen_via")
    @classmethod
    def _v_seen_via(cls, v: str) -> str:
        if v not in MEMBERSHIP_SOURCES:
            raise ValueError(f"seen_via must be one of {sorted(MEMBERSHIP_SOURCES)}")
        return v


class MulticastMembershipRead(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    ip_address_id: uuid.UUID
    role: str
    seen_via: str
    last_seen_at: datetime | None
    notes: str

    model_config = {"from_attributes": True}


class MulticastGroupCreate(BaseModel):
    space_id: uuid.UUID
    address: str = Field(..., min_length=1, max_length=45)
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    application: str = Field(default="", max_length=255)
    rtp_payload_type: int | None = Field(default=None, ge=0, le=127)
    bandwidth_mbps_estimate: Decimal | None = Field(default=None, ge=0)
    vlan_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    domain_id: uuid.UUID | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("address")
    @classmethod
    def _v_addr(cls, v: str) -> str:
        return _validate_multicast_addr(v)


class MulticastGroupUpdate(BaseModel):
    address: str | None = Field(default=None, min_length=1, max_length=45)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    application: str | None = Field(default=None, max_length=255)
    rtp_payload_type: int | None = Field(default=None, ge=0, le=127)
    bandwidth_mbps_estimate: Decimal | None = Field(default=None, ge=0)
    vlan_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    domain_id: uuid.UUID | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None

    @field_validator("address")
    @classmethod
    def _v_addr(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_multicast_addr(v)


class MulticastGroupRead(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    address: str
    name: str
    description: str
    application: str
    rtp_payload_type: int | None
    bandwidth_mbps_estimate: Decimal | None
    vlan_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    service_id: uuid.UUID | None
    domain_id: uuid.UUID | None
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    # asyncpg returns INET columns as ``ipaddress.IPv4Address`` /
    # ``IPv6Address`` rather than ``str``; coerce so the wire shape
    # (and Pydantic's ``str``-typed field) is happy. Same fix the ASN
    # / VRF code applied for prefix INET columns.
    @field_validator("address", mode="before")
    @classmethod
    def _addr_to_str(cls, v: Any) -> str:
        # ``address`` is NOT NULL in the model, but the validator
        # is defensive against a hypothetical None to satisfy mypy
        # without sprinkling ``cast`` calls at every call site.
        return str(v) if v is not None else ""


class MulticastGroupListResponse(BaseModel):
    items: list[MulticastGroupRead]
    total: int
    limit: int
    offset: int


# ── Helpers ─────────────────────────────────────────────────────────


async def _get_group(db: Any, group_id: uuid.UUID) -> MulticastGroup:
    row = await db.get(MulticastGroup, group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Multicast group not found")
    return row


async def _check_space(db: Any, space_id: uuid.UUID) -> None:
    if (await db.get(IPSpace, space_id)) is None:
        raise HTTPException(status_code=422, detail="space_id not found")


# ── Group endpoints ─────────────────────────────────────────────────


@router.get("/groups", response_model=MulticastGroupListResponse)
async def list_groups(
    db: DB,
    _: CurrentUser,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    space_id: uuid.UUID | None = Query(default=None),
    vlan_id: uuid.UUID | None = Query(default=None),
    customer_id: uuid.UUID | None = Query(default=None),
    service_id: uuid.UUID | None = Query(default=None),
    domain_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(
        default=None,
        description="Case-insensitive substring on name / application / address.",
    ),
    tag: list[str] = Query(default_factory=list),
) -> MulticastGroupListResponse:
    stmt = select(MulticastGroup)
    if space_id is not None:
        stmt = stmt.where(MulticastGroup.space_id == space_id)
    if vlan_id is not None:
        stmt = stmt.where(MulticastGroup.vlan_id == vlan_id)
    if customer_id is not None:
        stmt = stmt.where(MulticastGroup.customer_id == customer_id)
    if service_id is not None:
        stmt = stmt.where(MulticastGroup.service_id == service_id)
    if domain_id is not None:
        stmt = stmt.where(MulticastGroup.domain_id == domain_id)
    if search:
        needle = f"%{search.strip()}%"
        # ``address`` is INET; cast to text for ILIKE so partial-IP
        # searches ("239.5.7." → all 239.5.7.x groups) work without
        # needing a CIDR-aware contains operator.
        stmt = stmt.where(
            or_(
                MulticastGroup.name.ilike(needle),
                MulticastGroup.application.ilike(needle),
                func.host(MulticastGroup.address).ilike(needle),
            )
        )
    stmt = apply_tag_filter(stmt, MulticastGroup.tags, tag)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(MulticastGroup.address.asc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return MulticastGroupListResponse(
        items=[MulticastGroupRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/groups", response_model=MulticastGroupRead, status_code=status.HTTP_201_CREATED)
async def create_group(body: MulticastGroupCreate, db: DB, user: CurrentUser) -> MulticastGroupRead:
    await _check_space(db, body.space_id)

    row = MulticastGroup(
        space_id=body.space_id,
        address=body.address,
        name=body.name,
        description=body.description,
        application=body.application,
        rtp_payload_type=body.rtp_payload_type,
        bandwidth_mbps_estimate=body.bandwidth_mbps_estimate,
        vlan_id=body.vlan_id,
        customer_id=body.customer_id,
        service_id=body.service_id,
        domain_id=body.domain_id,
        tags=body.tags or {},
        custom_fields=body.custom_fields or {},
    )
    db.add(row)
    await db.flush()

    write_audit(
        db,
        user=user,
        action="create",
        resource_type="multicast_group",
        resource_id=str(row.id),
        resource_display=f"{row.name} ({row.address})",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return MulticastGroupRead.model_validate(row)


@router.get("/groups/{group_id:uuid}", response_model=MulticastGroupRead)
async def get_group(group_id: uuid.UUID, db: DB, _: CurrentUser) -> MulticastGroupRead:
    row = await _get_group(db, group_id)
    return MulticastGroupRead.model_validate(row)


@router.put("/groups/{group_id:uuid}", response_model=MulticastGroupRead)
async def update_group(
    group_id: uuid.UUID, body: MulticastGroupUpdate, db: DB, user: CurrentUser
) -> MulticastGroupRead:
    row = await _get_group(db, group_id)
    name_before = row.name
    addr_before = str(row.address)

    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(row, field, value)

    write_audit(
        db,
        user=user,
        action="update",
        resource_type="multicast_group",
        resource_id=str(row.id),
        resource_display=f"{name_before} ({addr_before})",
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_unset=True),
    )
    await db.commit()
    await db.refresh(row)
    return MulticastGroupRead.model_validate(row)


@router.delete("/groups/{group_id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    row = await _get_group(db, group_id)
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="multicast_group",
        resource_id=str(row.id),
        resource_display=f"{row.name} ({row.address})",
    )
    await db.delete(row)
    await db.commit()


# ── Port endpoints ──────────────────────────────────────────────────


@router.get("/groups/{group_id:uuid}/ports", response_model=list[MulticastGroupPortRead])
async def list_ports(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[MulticastGroupPortRead]:
    await _get_group(db, group_id)
    rows = (
        (
            await db.execute(
                select(MulticastGroupPort)
                .where(MulticastGroupPort.group_id == group_id)
                .order_by(MulticastGroupPort.port_start.asc())
            )
        )
        .scalars()
        .all()
    )
    return [MulticastGroupPortRead.model_validate(r) for r in rows]


@router.post(
    "/groups/{group_id:uuid}/ports",
    response_model=MulticastGroupPortRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_port(
    group_id: uuid.UUID,
    body: MulticastGroupPortCreate,
    db: DB,
    user: CurrentUser,
) -> MulticastGroupPortRead:
    group = await _get_group(db, group_id)
    row = MulticastGroupPort(
        group_id=group_id,
        port_start=body.port_start,
        port_end=body.port_end,
        transport=body.transport,
        notes=body.notes,
    )
    db.add(row)
    await db.flush()

    label = f"{body.port_start}" if body.port_end is None else f"{body.port_start}-{body.port_end}"
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="multicast_group_port",
        resource_id=str(row.id),
        resource_display=f"{group.name}: port {label}/{body.transport}",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return MulticastGroupPortRead.model_validate(row)


@router.delete("/ports/{port_id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_port(port_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    row = await db.get(MulticastGroupPort, port_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Port row not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="multicast_group_port",
        resource_id=str(row.id),
        resource_display=f"port {row.port_start}",
    )
    await db.delete(row)
    await db.commit()


# ── Membership endpoints ────────────────────────────────────────────


@router.get(
    "/groups/{group_id:uuid}/memberships",
    response_model=list[MulticastMembershipRead],
)
async def list_memberships(
    group_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[MulticastMembershipRead]:
    await _get_group(db, group_id)
    rows = (
        (
            await db.execute(
                select(MulticastMembership)
                .where(MulticastMembership.group_id == group_id)
                .order_by(MulticastMembership.role.asc(), MulticastMembership.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [MulticastMembershipRead.model_validate(r) for r in rows]


@router.post(
    "/groups/{group_id:uuid}/memberships",
    response_model=MulticastMembershipRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_membership(
    group_id: uuid.UUID,
    body: MulticastMembershipCreate,
    db: DB,
    user: CurrentUser,
) -> MulticastMembershipRead:
    group = await _get_group(db, group_id)
    if (await db.get(IPAddress, body.ip_address_id)) is None:
        raise HTTPException(status_code=422, detail="ip_address_id not found")

    # Catch the unique-triplet violation with a clean 409 instead of
    # a generic 500 on IntegrityError.
    existing = (
        await db.execute(
            select(MulticastMembership.id).where(
                MulticastMembership.group_id == group_id,
                MulticastMembership.ip_address_id == body.ip_address_id,
                MulticastMembership.role == body.role,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="Membership (group, ip, role) already exists",
        )

    row = MulticastMembership(
        group_id=group_id,
        ip_address_id=body.ip_address_id,
        role=body.role,
        seen_via=body.seen_via,
        notes=body.notes,
    )
    db.add(row)
    await db.flush()

    write_audit(
        db,
        user=user,
        action="create",
        resource_type="multicast_membership",
        resource_id=str(row.id),
        resource_display=f"{group.name}: {body.role}",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return MulticastMembershipRead.model_validate(row)


@router.delete(
    "/memberships/{membership_id:uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_membership(membership_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    row = await db.get(MulticastMembership, membership_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="multicast_membership",
        resource_id=str(row.id),
        resource_display=f"{row.role}",
    )
    await db.delete(row)
    await db.commit()


# ── Bulk allocate ─────────────────────────────────────────────────
#
# Stamp a contiguous run of N multicast addresses with templated
# names in one shot. Two endpoints — ``/preview`` (read-only blast
# radius, surfaces conflicts before the operator commits) and the
# bare commit (transactional, refuses if any conflicts remain).
#
# Grammar reuse — ``{n}`` / ``{n:03d}`` / ``{oct1-4}`` etc — is
# imported from the IPAM bulk-allocate path so the two stay in
# lock-step. Same upper cap on a single call shape (256 here vs
# 1024 there): the multicast registry is curated, not a sweep.

_BULK_MAX_GROUPS = 256


class MulticastBulkAllocateItem(BaseModel):
    address: str
    name: str
    conflict: str | None = None  # ``in_use`` when the address already exists in-space


class MulticastBulkAllocateRequest(BaseModel):
    space_id: uuid.UUID
    count: int = Field(..., ge=1, le=_BULK_MAX_GROUPS)
    name_template: str = Field(..., min_length=1, max_length=128)
    start_address: str = Field(..., min_length=1, max_length=45)
    template_start: int = Field(default=1, ge=0)
    application: str = Field(default="", max_length=255)
    description: str = ""
    vlan_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    domain_id: uuid.UUID | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("start_address")
    @classmethod
    def _v_start(cls, v: str) -> str:
        return _validate_multicast_addr(v)


class MulticastBulkAllocatePreviewResponse(BaseModel):
    items: list[MulticastBulkAllocateItem]
    conflict_count: int
    cap: int = _BULK_MAX_GROUPS


class MulticastBulkAllocateCommitResponse(BaseModel):
    created: int
    group_ids: list[uuid.UUID]


async def _build_bulk_candidates(
    db: Any, body: MulticastBulkAllocateRequest
) -> list[MulticastBulkAllocateItem]:
    """Walk the address sequence + flag in-use rows. Address-class
    is already validated by ``_v_start``; we only have to refuse
    when the run would step outside the multicast range (e.g. count
    too high to fit between start and the upper bound)."""
    # Reuse the IPAM-side template helpers — keeping one grammar +
    # one parser across both surfaces avoids drift. Local import to
    # dodge any module-load-order cost for endpoints that never use
    # bulk-allocate.
    from app.api.v1.ipam.router import _expand_bulk_template  # noqa: PLC0415

    base = ipaddress.ip_address(body.start_address)
    family = "v4" if isinstance(base, ipaddress.IPv4Address) else "v6"
    base_int = int(base)

    # Build the candidate addresses + verify they all stay inside
    # the multicast range (top of v4 range is 239.255.255.255 =
    # 4026531839; top of v6 ff::/8 range is well past anything
    # operators would ever bulk-stamp).
    upper_bound = (
        int(ipaddress.IPv4Address("239.255.255.255"))
        if family == "v4"
        else int(ipaddress.IPv6Address("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"))
    )
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for i in range(body.count):
        next_int = base_int + i
        if next_int > upper_bound:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"start_address + count - 1 walks past the multicast range "
                    f"(item {i + 1} of {body.count} would land at an out-of-range address)"
                ),
            )
        candidates.append(
            ipaddress.IPv4Address(next_int) if family == "v4" else ipaddress.IPv6Address(next_int)
        )

    # In-space already-allocated check. Single query against the
    # candidate set rather than N round-trips.
    addr_strings = [str(a) for a in candidates]
    used_rows = (
        await db.execute(
            select(MulticastGroup.address).where(
                MulticastGroup.space_id == body.space_id,
                MulticastGroup.address.in_(addr_strings),
            )
        )
    ).all()
    used: set[str] = {str(r[0]) for r in used_rows}

    items: list[MulticastBulkAllocateItem] = []
    for idx, ip_obj in enumerate(candidates):
        n = body.template_start + idx
        rendered_name = _expand_bulk_template(body.name_template, ip_obj, n)
        addr_str = str(ip_obj)
        items.append(
            MulticastBulkAllocateItem(
                address=addr_str,
                name=rendered_name,
                conflict=("in_use" if addr_str in used else None),
            )
        )
    return items


@router.post(
    "/groups/bulk-allocate/preview",
    response_model=MulticastBulkAllocatePreviewResponse,
)
async def bulk_allocate_preview(
    body: MulticastBulkAllocateRequest, db: DB, _: CurrentUser
) -> MulticastBulkAllocatePreviewResponse:
    """Read-only blast radius for a bulk-allocate. Surfaces address
    conflicts so the operator can change ``start_address`` /
    ``count`` before committing."""
    await _check_space(db, body.space_id)
    items = await _build_bulk_candidates(db, body)
    conflicts = sum(1 for it in items if it.conflict is not None)
    return MulticastBulkAllocatePreviewResponse(items=items, conflict_count=conflicts)


@router.post(
    "/groups/bulk-allocate/commit",
    response_model=MulticastBulkAllocateCommitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bulk_allocate_commit(
    body: MulticastBulkAllocateRequest, db: DB, user: CurrentUser
) -> MulticastBulkAllocateCommitResponse:
    """Commit a bulk-allocate. Refuses if any candidate address
    already has a multicast group in the same space — the operator
    runs ``/preview`` first to see + resolve conflicts."""
    await _check_space(db, body.space_id)
    items = await _build_bulk_candidates(db, body)
    conflicts = [it for it in items if it.conflict is not None]
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{len(conflicts)} address(es) already in use; "
                    "adjust start_address or count and re-preview"
                ),
                "conflicts": [it.address for it in conflicts],
            },
        )

    created_ids: list[uuid.UUID] = []
    for item in items:
        row = MulticastGroup(
            space_id=body.space_id,
            address=item.address,
            name=item.name,
            description=body.description,
            application=body.application,
            vlan_id=body.vlan_id,
            customer_id=body.customer_id,
            service_id=body.service_id,
            domain_id=body.domain_id,
            tags=body.tags or {},
            custom_fields=body.custom_fields or {},
        )
        db.add(row)
        await db.flush()
        created_ids.append(row.id)

    write_audit(
        db,
        user=user,
        action="bulk_allocate",
        resource_type="multicast_group",
        resource_id=str(body.space_id),
        resource_display=(f"{len(created_ids)} group(s) starting at {body.start_address}"),
        new_value={
            "count": len(created_ids),
            "start_address": body.start_address,
            "name_template": body.name_template,
            "space_id": str(body.space_id),
        },
    )
    await db.commit()
    return MulticastBulkAllocateCommitResponse(created=len(created_ids), group_ids=created_ids)
