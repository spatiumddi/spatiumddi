"""DHCP server CRUD + sync/approve/leases."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import DHCPConfigOp, DHCPLease, DHCPServer
from app.services.dhcp.config_bundle import build_config_bundle

router = APIRouter(prefix="/servers", tags=["dhcp"])

VALID_DRIVERS = {"kea", "isc_dhcp"}


class ServerCreate(BaseModel):
    name: str
    description: str = ""
    driver: str = "kea"
    host: str
    port: int = 67
    roles: list[str] = []
    server_group_id: uuid.UUID | None = None

    @field_validator("driver")
    @classmethod
    def _d(cls, v: str) -> str:
        if v not in VALID_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(VALID_DRIVERS)}")
        return v


class ServerUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    driver: str | None = None
    host: str | None = None
    port: int | None = None
    roles: list[str] | None = None
    server_group_id: uuid.UUID | None = None
    status: str | None = None

    @field_validator("driver")
    @classmethod
    def _d(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_DRIVERS:
            raise ValueError(f"driver must be one of {sorted(VALID_DRIVERS)}")
        return v


class ServerResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    driver: str
    host: str
    port: int
    roles: list[str]
    server_group_id: uuid.UUID | None
    status: str
    last_sync_at: datetime | None
    last_health_check_at: datetime | None
    agent_registered: bool
    agent_approved: bool
    agent_last_seen: datetime | None
    agent_version: str | None
    config_etag: str | None
    config_pushed_at: datetime | None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


class LeaseResponse(BaseModel):
    id: uuid.UUID
    server_id: uuid.UUID
    scope_id: uuid.UUID | None
    ip_address: str
    mac_address: str
    hostname: str | None
    state: str
    starts_at: datetime | None
    ends_at: datetime | None
    expires_at: datetime | None
    last_seen_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ServerResponse])
async def list_servers(db: DB, _: CurrentUser) -> list[DHCPServer]:
    res = await db.execute(select(DHCPServer).order_by(DHCPServer.name))
    return list(res.scalars().all())


@router.post("", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(body: ServerCreate, db: DB, user: SuperAdmin) -> DHCPServer:
    existing = await db.execute(select(DHCPServer).where(DHCPServer.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A DHCP server with that name exists")
    s = DHCPServer(**body.model_dump())
    db.add(s)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(s)
    return s


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(server_id: uuid.UUID, db: DB, _: CurrentUser) -> DHCPServer:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    return s


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: uuid.UUID, body: ServerUpdate, db: DB, user: SuperAdmin
) -> DHCPServer:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(s, k, v)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_none=True),
    )
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
    )
    await db.delete(s)
    await db.commit()


@router.post("/{server_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_server(server_id: uuid.UUID, db: DB, user: SuperAdmin) -> dict[str, str]:
    """Force a config push: rebuild the bundle, enqueue an apply_config op."""
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    bundle = await build_config_bundle(db, s)
    s.config_etag = bundle.etag
    op = DHCPConfigOp(
        server_id=s.id,
        op_type="apply_config",
        payload={"etag": bundle.etag},
        status="pending",
    )
    db.add(op)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="dhcp.server.sync",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
        new_value={"etag": bundle.etag, "op_id": str(op.id)},
    )
    await db.commit()
    return {"status": "queued", "op_id": str(op.id), "etag": bundle.etag}


@router.post("/{server_id}/approve", response_model=ServerResponse)
async def approve_server(server_id: uuid.UUID, db: DB, user: SuperAdmin) -> DHCPServer:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    s.agent_approved = True
    write_audit(
        db,
        user=user,
        action="dhcp.server.approve",
        resource_type="dhcp_server",
        resource_id=str(s.id),
        resource_display=s.name,
    )
    await db.commit()
    await db.refresh(s)
    return s


@router.get("/{server_id}/leases", response_model=list[LeaseResponse])
async def list_leases(
    server_id: uuid.UUID, db: DB, _: CurrentUser, limit: int = 500
) -> list[DHCPLease]:
    s = await db.get(DHCPServer, server_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Server not found")
    res = await db.execute(
        select(DHCPLease)
        .where(DHCPLease.server_id == server_id)
        .order_by(DHCPLease.last_seen_at.desc())
        .limit(min(limit, 5000))
    )
    return list(res.scalars().all())
