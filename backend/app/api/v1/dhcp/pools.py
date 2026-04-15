"""DHCP pool CRUD under /scopes/{scope_id}/pools."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import DHCPPool, DHCPScope

router = APIRouter(tags=["dhcp"])

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
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("start_ip", "end_ip", mode="before")
    @classmethod
    def _inet_to_str(cls, v: Any) -> Any:
        return str(v) if v is not None else v


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
) -> DHCPPool:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
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
    return pool


@router.put("/pools/{pool_id}", response_model=PoolResponse)
async def update_pool(
    pool_id: uuid.UUID, body: PoolUpdate, db: DB, user: SuperAdmin
) -> DHCPPool:
    pool = await db.get(DHCPPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
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
