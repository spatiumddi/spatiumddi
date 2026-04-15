"""DHCP server group CRUD."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import DHCPServerGroup

router = APIRouter(prefix="/server-groups", tags=["dhcp"])

VALID_MODES = {"standalone", "load-balancing", "hot-standby"}


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    mode: str = "hot-standby"

    @field_validator("mode")
    @classmethod
    def _m(cls, v: str) -> str:
        if v not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        return v


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    mode: str | None = None

    @field_validator("mode")
    @classmethod
    def _m(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        return v


class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    mode: str
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[GroupResponse])
async def list_groups(db: DB, _: CurrentUser) -> list[DHCPServerGroup]:
    res = await db.execute(select(DHCPServerGroup).order_by(DHCPServerGroup.name))
    return list(res.scalars().all())


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(body: GroupCreate, db: DB, user: SuperAdmin) -> DHCPServerGroup:
    existing = await db.execute(select(DHCPServerGroup).where(DHCPServerGroup.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A DHCP server group with that name exists")
    g = DHCPServerGroup(**body.model_dump())
    db.add(g)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_server_group",
        resource_id=str(g.id),
        resource_display=g.name,
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(g)
    return g


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(group_id: uuid.UUID, db: DB, _: CurrentUser) -> DHCPServerGroup:
    g = await db.get(DHCPServerGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Server group not found")
    return g


@router.put("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: uuid.UUID, body: GroupUpdate, db: DB, user: SuperAdmin
) -> DHCPServerGroup:
    g = await db.get(DHCPServerGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Server group not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(g, k, v)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_server_group",
        resource_id=str(g.id),
        resource_display=g.name,
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_none=True),
    )
    await db.commit()
    await db.refresh(g)
    return g


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    g = await db.get(DHCPServerGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Server group not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_server_group",
        resource_id=str(g.id),
        resource_display=g.name,
    )
    await db.delete(g)
    await db.commit()
