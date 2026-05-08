"""Multicast group CRUD — issue #126 Phase 1.

Three sub-resources behind one router prefix:

* ``/multicast/groups`` — group create / list / read / update / delete
* ``/multicast/groups/{id}/ports`` — port range CRUD on a group
* ``/multicast/groups/{id}/memberships`` — producer/consumer/RP rows
* ``/multicast/memberships/{id}`` — direct delete (the membership id
  is operator-visible in lists, so a flat delete URL avoids forcing
  a round-trip to look up the group_id)

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

Phase 1 is registry-only. Bulk-allocate (Wave 3), the Conformity
collision check (Wave 3), and Operator Copilot tools (Phase 4)
land in follow-up commits.
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
