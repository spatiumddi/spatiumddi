"""DNS Pool (GSLB-lite) CRUD.

Pools live under a zone; members live under a pool. The actual
``DNSRecord`` rows that make the pool live are owned by the pool
apply-state service and flagged with ``pool_member_id`` — this router
only manages the pool config + member roster + manual triggers.

Permission gate: ``manage_dns_pools`` (admin-only). Seeded into the
existing "DNS Editor" builtin role at startup.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.core.permissions import require_resource_permission
from app.models.audit import AuditLog
from app.models.dns import DNSPool, DNSPoolMember, DNSZone
from app.services.dns.pool_apply import apply_pool_state

logger = structlog.get_logger(__name__)

router = APIRouter(
    tags=["dns-pools"],
    dependencies=[Depends(require_resource_permission("manage_dns_pools"))],
)


VALID_HC_TYPES = {"none", "tcp", "http", "https", "icmp"}
VALID_RECORD_TYPES = {"A", "AAAA"}


# ── Schemas ───────────────────────────────────────────────────────────────


class PoolMemberWrite(BaseModel):
    address: str
    weight: int = 1
    enabled: bool = True

    @field_validator("address")
    @classmethod
    def _addr(cls, v: str) -> str:
        v = v.strip()
        try:
            ipaddress.ip_address(v)
        except ValueError as exc:
            raise ValueError(f"invalid IP address: {v}") from exc
        return v


class PoolMemberResponse(BaseModel):
    id: uuid.UUID
    pool_id: uuid.UUID
    address: str
    weight: int
    enabled: bool
    last_check_state: str
    last_check_at: datetime | None
    last_check_error: str | None
    consecutive_failures: int
    consecutive_successes: int
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


class PoolWrite(BaseModel):
    name: str
    description: str = ""
    record_name: str
    record_type: str = "A"
    ttl: int = Field(default=30, ge=1, le=86400)
    enabled: bool = True
    hc_type: str = "tcp"
    hc_target_port: int | None = None
    hc_path: str = "/"
    hc_method: str = "GET"
    hc_verify_tls: bool = False
    hc_expected_status_codes: list[int] = Field(
        default_factory=lambda: [200, 201, 202, 204, 301, 302, 304]
    )
    hc_interval_seconds: int = Field(default=30, ge=10, le=3600)
    hc_timeout_seconds: int = Field(default=5, ge=1, le=60)
    hc_unhealthy_threshold: int = Field(default=2, ge=1, le=20)
    hc_healthy_threshold: int = Field(default=2, ge=1, le=20)
    members: list[PoolMemberWrite] | None = None

    @field_validator("record_type")
    @classmethod
    def _rtype(cls, v: str) -> str:
        v = v.upper()
        if v not in VALID_RECORD_TYPES:
            raise ValueError(f"record_type must be one of {sorted(VALID_RECORD_TYPES)}")
        return v

    @field_validator("hc_type")
    @classmethod
    def _hctype(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_HC_TYPES:
            raise ValueError(f"hc_type must be one of {sorted(VALID_HC_TYPES)}")
        return v


class PoolUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=86400)
    enabled: bool | None = None
    hc_type: str | None = None
    hc_target_port: int | None = None
    hc_path: str | None = None
    hc_method: str | None = None
    hc_verify_tls: bool | None = None
    hc_expected_status_codes: list[int] | None = None
    hc_interval_seconds: int | None = Field(default=None, ge=10, le=3600)
    hc_timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    hc_unhealthy_threshold: int | None = Field(default=None, ge=1, le=20)
    hc_healthy_threshold: int | None = Field(default=None, ge=1, le=20)

    @field_validator("hc_type")
    @classmethod
    def _hctype(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.lower()
        if v not in VALID_HC_TYPES:
            raise ValueError(f"hc_type must be one of {sorted(VALID_HC_TYPES)}")
        return v


class PoolResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    zone_id: uuid.UUID
    name: str
    description: str
    record_name: str
    record_type: str
    ttl: int
    enabled: bool
    hc_type: str
    hc_target_port: int | None
    hc_path: str
    hc_method: str
    hc_verify_tls: bool
    hc_expected_status_codes: list[int]
    hc_interval_seconds: int
    hc_timeout_seconds: int
    hc_unhealthy_threshold: int
    hc_healthy_threshold: int
    next_check_at: datetime | None
    last_checked_at: datetime | None
    members: list[PoolMemberResponse]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


def _pool_to_response(p: DNSPool) -> PoolResponse:
    return PoolResponse(
        id=p.id,
        group_id=p.group_id,
        zone_id=p.zone_id,
        name=p.name,
        description=p.description,
        record_name=p.record_name,
        record_type=p.record_type,
        ttl=p.ttl,
        enabled=p.enabled,
        hc_type=p.hc_type,
        hc_target_port=p.hc_target_port,
        hc_path=p.hc_path,
        hc_method=p.hc_method,
        hc_verify_tls=p.hc_verify_tls,
        hc_expected_status_codes=list(p.hc_expected_status_codes or []),
        hc_interval_seconds=p.hc_interval_seconds,
        hc_timeout_seconds=p.hc_timeout_seconds,
        hc_unhealthy_threshold=p.hc_unhealthy_threshold,
        hc_healthy_threshold=p.hc_healthy_threshold,
        next_check_at=p.next_check_at,
        last_checked_at=p.last_checked_at,
        members=[
            PoolMemberResponse.model_validate(m, from_attributes=True) for m in (p.members or [])
        ],
        created_at=p.created_at,
        modified_at=p.modified_at,
    )


# ── Pool CRUD ─────────────────────────────────────────────────────────────


class PoolListEntry(BaseModel):
    """Cross-zone listing row — pool metadata + zone/group context.

    Used by the top-level "DNS Pools" admin page where the operator
    isn't already in a zone's context. Members are summarised
    (counts + live count) rather than fully embedded so the
    page-of-many-pools response stays small.
    """

    id: uuid.UUID
    group_id: uuid.UUID
    group_name: str
    zone_id: uuid.UUID
    zone_name: str
    name: str
    description: str
    record_name: str
    record_type: str
    ttl: int
    enabled: bool
    hc_type: str
    hc_target_port: int | None
    hc_interval_seconds: int
    next_check_at: datetime | None
    last_checked_at: datetime | None
    member_count: int
    healthy_count: int
    enabled_count: int
    live_count: int
    created_at: datetime
    modified_at: datetime


@router.get("/pools", response_model=list[PoolListEntry])
async def list_all_pools(
    db: DB,
    _: CurrentUser,
    group_id: uuid.UUID | None = None,
) -> list[PoolListEntry]:
    """Cross-zone listing — every pool the user can see.

    Optional ``group_id`` filter scopes to one server group. Powers
    the top-level Pools admin page. Embeds zone + group names so the
    UI doesn't need to fetch them separately.
    """
    from app.models.dns import DNSServerGroup  # noqa: PLC0415

    stmt = select(DNSPool).order_by(DNSPool.name)
    if group_id is not None:
        stmt = stmt.where(DNSPool.group_id == group_id)
    pools = list((await db.execute(stmt)).scalars().all())
    if not pools:
        return []

    zone_ids = {p.zone_id for p in pools}
    group_ids = {p.group_id for p in pools}

    zones = (await db.execute(select(DNSZone).where(DNSZone.id.in_(zone_ids)))).scalars().all()
    zone_name_by_id = {z.id: z.name for z in zones}
    groups = (
        (await db.execute(select(DNSServerGroup).where(DNSServerGroup.id.in_(group_ids))))
        .scalars()
        .all()
    )
    group_name_by_id = {g.id: g.name for g in groups}

    entries: list[PoolListEntry] = []
    for p in pools:
        members = list(p.members or [])
        healthy = sum(1 for m in members if m.last_check_state == "healthy")
        enabled_n = sum(1 for m in members if m.enabled)
        live = sum(1 for m in members if m.enabled and m.last_check_state == "healthy")
        entries.append(
            PoolListEntry(
                id=p.id,
                group_id=p.group_id,
                group_name=group_name_by_id.get(p.group_id, str(p.group_id)),
                zone_id=p.zone_id,
                zone_name=zone_name_by_id.get(p.zone_id, str(p.zone_id)),
                name=p.name,
                description=p.description,
                record_name=p.record_name,
                record_type=p.record_type,
                ttl=p.ttl,
                enabled=p.enabled,
                hc_type=p.hc_type,
                hc_target_port=p.hc_target_port,
                hc_interval_seconds=p.hc_interval_seconds,
                next_check_at=p.next_check_at,
                last_checked_at=p.last_checked_at,
                member_count=len(members),
                healthy_count=healthy,
                enabled_count=enabled_n,
                live_count=live,
                created_at=p.created_at,
                modified_at=p.modified_at,
            )
        )
    return entries


@router.get(
    "/groups/{group_id}/zones/{zone_id}/pools",
    response_model=list[PoolResponse],
)
async def list_pools_in_zone(
    group_id: uuid.UUID, zone_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[PoolResponse]:
    res = await db.execute(
        select(DNSPool)
        .where(DNSPool.group_id == group_id, DNSPool.zone_id == zone_id)
        .order_by(DNSPool.name)
    )
    return [_pool_to_response(p) for p in res.scalars().all()]


@router.post(
    "/groups/{group_id}/zones/{zone_id}/pools",
    response_model=PoolResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pool(
    group_id: uuid.UUID,
    zone_id: uuid.UUID,
    body: PoolWrite,
    db: DB,
    user: SuperAdmin,
) -> PoolResponse:
    zone = await db.get(DNSZone, zone_id)
    if zone is None or zone.group_id != group_id:
        raise HTTPException(status_code=404, detail="Zone not found")
    if zone.zone_type == "forward":
        raise HTTPException(status_code=400, detail="Pools are not supported on forward zones")
    clash = await db.execute(
        select(DNSPool).where(DNSPool.zone_id == zone_id, DNSPool.record_name == body.record_name)
    )
    if clash.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"A pool already owns the record name {body.record_name!r} in this zone",
        )

    payload = body.model_dump(exclude={"members"})
    pool = DNSPool(group_id=group_id, zone_id=zone_id, **payload)
    db.add(pool)
    await db.flush()

    for m in body.members or []:
        db.add(DNSPoolMember(pool_id=pool.id, **m.model_dump()))

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="dns.pool.create",
            resource_type="dns_pool",
            resource_id=str(pool.id),
            resource_display=f"{pool.record_name}.{zone.name}",
            result="success",
        )
    )
    await db.commit()
    await db.refresh(pool)
    return _pool_to_response(pool)


@router.get("/pools/{pool_id}", response_model=PoolResponse)
async def get_pool(pool_id: uuid.UUID, db: DB, _: CurrentUser) -> PoolResponse:
    pool = await db.get(DNSPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    return _pool_to_response(pool)


@router.put("/pools/{pool_id}", response_model=PoolResponse)
async def update_pool(
    pool_id: uuid.UUID, body: PoolUpdate, db: DB, user: SuperAdmin
) -> PoolResponse:
    pool = await db.get(DNSPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    payload = body.model_dump(exclude_none=True)
    for k, v in payload.items():
        setattr(pool, k, v)
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="dns.pool.update",
            resource_type="dns_pool",
            resource_id=str(pool.id),
            resource_display=pool.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(pool)
    return _pool_to_response(pool)


@router.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(pool_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    pool = await db.get(DNSPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="dns.pool.delete",
            resource_type="dns_pool",
            resource_id=str(pool.id),
            resource_display=pool.name,
            result="success",
        )
    )
    # Members + the rendered DNSRecord rows cascade via FK ON DELETE CASCADE.
    # The records the pool created don't need explicit RFC 2136 deletes
    # here — the next health-check pass on a now-empty pool would bump
    # the zone serial, and AXFR pull would catch the deletion. Cleaner:
    # explicitly enqueue deletes via apply_pool_state before destroying
    # the rows.
    pool.members = []  # detach so apply emits deletes
    await apply_pool_state(db, pool)
    await db.delete(pool)
    await db.commit()


@router.post("/pools/{pool_id}/check-now", response_model=PoolResponse)
async def check_pool_now(pool_id: uuid.UUID, db: DB, user: SuperAdmin) -> PoolResponse:
    """Force ``next_check_at`` to ``now`` so the next dispatcher tick fires.

    Returns the updated pool — operators see ``next_check_at`` shift
    to the past, which is a clear visual cue that the check is queued.
    """
    pool = await db.get(DNSPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    pool.next_check_at = datetime.utcnow()
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="dns.pool.check_now",
            resource_type="dns_pool",
            resource_id=str(pool.id),
            resource_display=pool.name,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(pool)
    return _pool_to_response(pool)


# ── Member CRUD ───────────────────────────────────────────────────────────


@router.post(
    "/pools/{pool_id}/members",
    response_model=PoolMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    pool_id: uuid.UUID, body: PoolMemberWrite, db: DB, user: SuperAdmin
) -> PoolMemberResponse:
    pool = await db.get(DNSPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    clash = await db.execute(
        select(DNSPoolMember).where(
            DNSPoolMember.pool_id == pool_id,
            DNSPoolMember.address == body.address,
        )
    )
    if clash.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Member with address {body.address!r} already exists",
        )
    member = DNSPoolMember(pool_id=pool_id, **body.model_dump())
    db.add(member)
    await db.flush()
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="dns.pool.member.add",
            resource_type="dns_pool_member",
            resource_id=str(member.id),
            resource_display=f"{member.address} → {pool.name}",
            result="success",
        )
    )
    await db.commit()
    await db.refresh(member)
    return PoolMemberResponse.model_validate(member, from_attributes=True)


class PoolMemberUpdate(BaseModel):
    address: str | None = None
    weight: int | None = None
    enabled: bool | None = None

    @field_validator("address")
    @classmethod
    def _addr(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        try:
            ipaddress.ip_address(v)
        except ValueError as exc:
            raise ValueError(f"invalid IP address: {v}") from exc
        return v


@router.put("/pool-members/{member_id}", response_model=PoolMemberResponse)
async def update_member(
    member_id: uuid.UUID, body: PoolMemberUpdate, db: DB, user: SuperAdmin
) -> PoolMemberResponse:
    member = await db.get(DNSPoolMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Pool member not found")

    payload: dict[str, Any] = body.model_dump(exclude_none=True)
    # Snapshot the fields the reconciler diffs on so we can decide
    # below whether to re-render the rrset. Address edits in particular
    # used to fall through silently — the DB row updated but BIND9 kept
    # serving the old IP because nothing pushed a DDNS update.
    member_changed = any(
        k in payload and getattr(member, k) != payload[k] for k in ("address", "enabled", "weight")
    )

    # Uniqueness guard on address change. The DB has
    # ``UniqueConstraint("pool_id", "address")``, so without this we'd
    # blow up with an IntegrityError on commit; nicer to surface 409.
    new_address = payload.get("address")
    address_changed = new_address is not None and new_address != member.address
    if address_changed:
        clash = await db.execute(
            select(DNSPoolMember).where(
                DNSPoolMember.pool_id == member.pool_id,
                DNSPoolMember.address == new_address,
                DNSPoolMember.id != member.id,
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Member with address {new_address!r} already exists in this pool",
            )

    for k, v in payload.items():
        setattr(member, k, v)

    # Reset health stats when the IP itself changed — the new endpoint
    # has to earn its way back into the rrset rather than inheriting
    # the old IP's "healthy" state. ``unknown`` is the default starting
    # point already used by ``add_member``; the next health-check tick
    # will update it.
    if address_changed:
        member.last_check_state = "unknown"
        member.last_check_at = None
        member.last_check_error = None
        member.consecutive_successes = 0
        member.consecutive_failures = 0
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="dns.pool.member.update",
            resource_type="dns_pool_member",
            resource_id=str(member.id),
            resource_display=member.address,
            result="success",
        )
    )

    # Reconcile on any rrset-affecting change (address or enabled);
    # weight is advisory today but cheap to include if/when weighted
    # rendering lands. Don't make the operator wait for the next
    # health-check tick.
    if member_changed:
        pool = await db.get(DNSPool, member.pool_id)
        if pool is not None:
            await apply_pool_state(db, pool)

    await db.commit()
    await db.refresh(member)
    return PoolMemberResponse.model_validate(member, from_attributes=True)


@router.delete("/pool-members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_member(member_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    member = await db.get(DNSPoolMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Pool member not found")
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="dns.pool.member.delete",
            resource_type="dns_pool_member",
            resource_id=str(member.id),
            resource_display=member.address,
            result="success",
        )
    )
    # Make sure the rendered DNSRecord row goes away before the FK
    # cascade fires (so we get a proper enqueue_record_op delete).
    pool = await db.get(DNSPool, member.pool_id)
    if pool is not None:
        pool.members = [m for m in pool.members if m.id != member.id]
        await apply_pool_state(db, pool)
    await db.delete(member)
    await db.commit()
