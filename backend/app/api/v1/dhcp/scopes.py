"""DHCP scope CRUD. Scoped under /subnets/{subnet_id}/dhcp-scopes and /scopes/{id}."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import DHCPScope, DHCPServer
from app.models.ipam import Subnet

router = APIRouter(tags=["dhcp"])

VALID_HOSTNAME_POLICIES = {"client", "server_name", "derived", "none"}
VALID_SYNC_MODES = {"disabled", "on_lease", "on_static_only"}


class ScopeCreate(BaseModel):
    server_id: uuid.UUID
    is_active: bool = True
    lease_time: int = 86400
    min_lease_time: int | None = None
    max_lease_time: int | None = None
    options: dict[str, Any] = {}
    ddns_enabled: bool = False
    ddns_hostname_policy: str = "client"
    hostname_to_ipam_sync: str = "on_static_only"

    @field_validator("ddns_hostname_policy")
    @classmethod
    def _h(cls, v: str) -> str:
        if v not in VALID_HOSTNAME_POLICIES:
            raise ValueError(f"ddns_hostname_policy must be one of {sorted(VALID_HOSTNAME_POLICIES)}")
        return v

    @field_validator("hostname_to_ipam_sync")
    @classmethod
    def _s(cls, v: str) -> str:
        if v not in VALID_SYNC_MODES:
            raise ValueError(f"hostname_to_ipam_sync must be one of {sorted(VALID_SYNC_MODES)}")
        return v


class ScopeUpdate(BaseModel):
    is_active: bool | None = None
    lease_time: int | None = None
    min_lease_time: int | None = None
    max_lease_time: int | None = None
    options: dict[str, Any] | None = None
    ddns_enabled: bool | None = None
    ddns_hostname_policy: str | None = None
    hostname_to_ipam_sync: str | None = None


class ScopeResponse(BaseModel):
    id: uuid.UUID
    server_id: uuid.UUID
    subnet_id: uuid.UUID
    is_active: bool
    lease_time: int
    min_lease_time: int | None
    max_lease_time: int | None
    options: dict[str, Any]
    ddns_enabled: bool
    ddns_hostname_policy: str
    hostname_to_ipam_sync: str
    last_pushed_at: datetime | None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


@router.get("/subnets/{subnet_id}/dhcp-scopes", response_model=list[ScopeResponse])
async def list_scopes_for_subnet(
    subnet_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[DHCPScope]:
    res = await db.execute(select(DHCPScope).where(DHCPScope.subnet_id == subnet_id))
    return list(res.scalars().all())


@router.post(
    "/subnets/{subnet_id}/dhcp-scopes",
    response_model=ScopeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_scope(
    subnet_id: uuid.UUID, body: ScopeCreate, db: DB, user: SuperAdmin
) -> DHCPScope:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    srv = await db.get(DHCPServer, body.server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="DHCP server not found")
    existing = await db.execute(
        select(DHCPScope).where(
            DHCPScope.server_id == body.server_id, DHCPScope.subnet_id == subnet_id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A scope for this server+subnet already exists"
        )
    scope = DHCPScope(subnet_id=subnet_id, **body.model_dump())
    db.add(scope)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=f"{srv.name}:{subnet.network}",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(scope)
    return scope


@router.get("/scopes/{scope_id}", response_model=ScopeResponse)
async def get_scope(scope_id: uuid.UUID, db: DB, _: CurrentUser) -> DHCPScope:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    return scope


@router.put("/scopes/{scope_id}", response_model=ScopeResponse)
async def update_scope(
    scope_id: uuid.UUID, body: ScopeUpdate, db: DB, user: SuperAdmin
) -> DHCPScope:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(scope, k, v)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=str(scope.id),
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_none=True),
    )
    await db.commit()
    await db.refresh(scope)
    return scope


@router.delete("/scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scope(scope_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_scope",
        resource_id=str(scope.id),
        resource_display=str(scope.id),
    )
    await db.delete(scope)
    await db.commit()
