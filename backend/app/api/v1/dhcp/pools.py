"""DHCP pool CRUD under /scopes/{scope_id}/pools."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.dhcp import DHCPPool, DHCPScope
from app.models.ipam import IPAddress

router = APIRouter(tags=["dhcp"], dependencies=[Depends(require_resource_permission("dhcp_pool"))])

VALID_POOL_TYPES = {"dynamic", "excluded", "reserved"}


class PoolCreate(BaseModel):
    name: str = ""
    start_ip: str
    end_ip: str
    pool_type: str = "dynamic"
    class_restriction: str | None = None
    lease_time_override: int | None = None
    options_override: dict[str, Any] | None = None

    @field_validator("pool_type")
    @classmethod
    def _p(cls, v: str) -> str:
        if v not in VALID_POOL_TYPES:
            raise ValueError(f"pool_type must be one of {sorted(VALID_POOL_TYPES)}")
        return v


class PoolUpdate(BaseModel):
    name: str | None = None
    start_ip: str | None = None
    end_ip: str | None = None
    pool_type: str | None = None
    class_restriction: str | None = None
    lease_time_override: int | None = None
    options_override: dict[str, Any] | None = None


class PoolResponse(BaseModel):
    id: uuid.UUID
    scope_id: uuid.UUID
    name: str
    start_ip: str
    end_ip: str
    pool_type: str
    class_restriction: str | None
    lease_time_override: int | None
    options_override: dict[str, Any] | None
    existing_ips_in_range: list[dict[str, str]] | None = None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("start_ip", "end_ip", mode="before")
    @classmethod
    def _inet_to_str(cls, v: Any) -> Any:
        return str(v) if v is not None else v


def _ip_int(ip_str: str) -> int:
    return int(ipaddress.IPv4Address(ip_str))


async def _check_pool_overlap(
    db: AsyncSession,
    scope_id: uuid.UUID,
    start: str,
    end: str,
    exclude_id: uuid.UUID | None = None,
) -> str | None:
    """Return an error message if the given range overlaps any existing pool in the scope."""
    new_start, new_end = _ip_int(start), _ip_int(end)
    if new_start > new_end:
        return f"start_ip ({start}) must be <= end_ip ({end})"
    res = await db.execute(select(DHCPPool).where(DHCPPool.scope_id == scope_id))
    for p in res.scalars().all():
        if exclude_id and p.id == exclude_id:
            continue
        ps, pe = _ip_int(str(p.start_ip)), _ip_int(str(p.end_ip))
        if new_start <= pe and new_end >= ps:
            return (
                f"Range {start}–{end} overlaps existing pool "
                f"'{p.name or p.id}' ({p.start_ip}–{p.end_ip})"
            )
    return None


async def _existing_ips_in_range(
    db: AsyncSession, subnet_id: uuid.UUID, start: str, end: str
) -> list[dict[str, str]]:
    """Return IPAM addresses that fall inside the given range and aren't 'available'."""
    res = await db.execute(select(IPAddress).where(IPAddress.subnet_id == subnet_id))
    s, e = _ip_int(start), _ip_int(end)
    hits: list[dict[str, str]] = []
    for ip in res.scalars().all():
        v = _ip_int(str(ip.address))
        if s <= v <= e and ip.status not in ("available", "network", "broadcast"):
            hits.append(
                {
                    "address": str(ip.address),
                    "status": ip.status,
                    "hostname": ip.hostname or "",
                }
            )
    return hits


@router.get("/scopes/{scope_id}/pools", response_model=list[PoolResponse])
async def list_pools(scope_id: uuid.UUID, db: DB, _: CurrentUser) -> list[DHCPPool]:
    res = await db.execute(select(DHCPPool).where(DHCPPool.scope_id == scope_id))
    return list(res.scalars().all())


@router.post(
    "/scopes/{scope_id}/pools",
    response_model=PoolResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pool(
    scope_id: uuid.UUID, body: PoolCreate, db: DB, user: SuperAdmin
) -> PoolResponse:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    overlap = await _check_pool_overlap(db, scope_id, body.start_ip, body.end_ip)
    if overlap:
        raise HTTPException(status_code=409, detail=overlap)
    pool = DHCPPool(scope_id=scope_id, **body.model_dump())
    db.add(pool)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_pool",
        resource_id=str(pool.id),
        resource_display=f"{body.start_ip}-{body.end_ip}",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(pool)
    existing = await _existing_ips_in_range(db, scope.subnet_id, body.start_ip, body.end_ip)
    resp = PoolResponse.model_validate(pool, from_attributes=True)
    resp.existing_ips_in_range = existing or None
    return resp


@router.put("/pools/{pool_id}", response_model=PoolResponse)
async def update_pool(pool_id: uuid.UUID, body: PoolUpdate, db: DB, user: SuperAdmin) -> DHCPPool:
    pool = await db.get(DHCPPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    new_start = body.start_ip or str(pool.start_ip)
    new_end = body.end_ip or str(pool.end_ip)
    if body.start_ip or body.end_ip:
        overlap = await _check_pool_overlap(
            db, pool.scope_id, new_start, new_end, exclude_id=pool.id
        )
        if overlap:
            raise HTTPException(status_code=409, detail=overlap)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(pool, k, v)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_pool",
        resource_id=str(pool.id),
        resource_display=f"{pool.start_ip}-{pool.end_ip}",
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_none=True),
    )
    await db.commit()
    await db.refresh(pool)
    return pool


@router.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(pool_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    pool = await db.get(DHCPPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_pool",
        resource_id=str(pool.id),
        resource_display=f"{pool.start_ip}-{pool.end_ip}",
    )
    await db.delete(pool)
    await db.commit()
