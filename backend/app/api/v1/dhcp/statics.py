"""DHCP static assignment CRUD + conflict detection."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.models.dhcp import DHCPPool, DHCPScope, DHCPStaticAssignment
from app.models.ipam import IPAddress, Subnet

router = APIRouter(tags=["dhcp"])


class StaticCreate(BaseModel):
    ip_address: str
    mac_address: str
    hostname: str = ""
    description: str = ""
    client_id: str | None = None
    options_override: dict[str, Any] | None = None
    ip_address_id: uuid.UUID | None = None


class StaticUpdate(BaseModel):
    ip_address: str | None = None
    mac_address: str | None = None
    hostname: str | None = None
    description: str | None = None
    client_id: str | None = None
    options_override: dict[str, Any] | None = None
    ip_address_id: uuid.UUID | None = None


class StaticResponse(BaseModel):
    id: uuid.UUID
    scope_id: uuid.UUID
    ip_address: str
    mac_address: str
    hostname: str
    description: str
    client_id: str | None
    options_override: dict[str, Any] | None
    ip_address_id: uuid.UUID | None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("ip_address", "mac_address", mode="before")
    @classmethod
    def _inet_mac_to_str(cls, v: Any) -> Any:
        return str(v) if v is not None else v


async def _upsert_ipam_for_static(
    db, scope: DHCPScope, st: DHCPStaticAssignment
) -> None:
    """Create or update the IPAM row mirroring a static DHCP assignment.

    The static is the source of truth for hostname/MAC; IPAM reflects it with
    ``status='static_dhcp'`` and a back-link via ``static_assignment_id`` so the
    subnet view shows the reservation alongside regular addresses.
    """
    ip_str = str(st.ip_address)
    # Detach any previous IPAM row that was pointing at this static (IP change).
    prior = await db.execute(
        select(IPAddress).where(IPAddress.static_assignment_id == str(st.id))
    )
    for row in prior.scalars().all():
        if str(row.address) == ip_str:
            continue
        row.static_assignment_id = None
        if row.status == "static_dhcp":
            row.status = "allocated"
    # Find or create the IPAM row for this IP within the scope's subnet.
    res = await db.execute(
        select(IPAddress).where(
            IPAddress.subnet_id == scope.subnet_id, IPAddress.address == ip_str
        )
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = IPAddress(subnet_id=scope.subnet_id, address=ip_str, status="static_dhcp")
        db.add(row)
    row.hostname = st.hostname or row.hostname
    row.mac_address = str(st.mac_address)
    row.status = "static_dhcp"
    row.static_assignment_id = str(st.id)
    await db.flush()
    st.ip_address_id = row.id


async def _detach_ipam_for_static(db, st: DHCPStaticAssignment) -> None:
    """Release the IPAM row back to `allocated` when the static is removed."""
    res = await db.execute(
        select(IPAddress).where(IPAddress.static_assignment_id == str(st.id))
    )
    for row in res.scalars().all():
        row.static_assignment_id = None
        if row.status == "static_dhcp":
            row.status = "allocated"


async def _conflict_check(
    db, scope: DHCPScope, ip: str, mac: str, exclude_id: uuid.UUID | None = None
) -> None:
    """Conflict: same MAC on same server (across scopes), IP inside a reserved/dynamic pool on another scope+server."""
    # MAC dup across same server's scopes
    same_mac = await db.execute(
        select(DHCPStaticAssignment)
        .join(DHCPScope, DHCPStaticAssignment.scope_id == DHCPScope.id)
        .where(
            DHCPScope.server_id == scope.server_id,
            DHCPStaticAssignment.mac_address == mac,
        )
    )
    for row in same_mac.scalars().all():
        if exclude_id is not None and row.id == exclude_id:
            continue
        raise HTTPException(
            status_code=409,
            detail=f"MAC {mac} already reserved on this server in scope {row.scope_id}",
        )

    # IP inside existing pool of this scope — reject if dynamic
    try:
        ip_addr = ipaddress.ip_address(ip)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid IP: {e}") from e
    pools_res = await db.execute(select(DHCPPool).where(DHCPPool.scope_id == scope.id))
    for p in pools_res.scalars().all():
        try:
            start = ipaddress.ip_address(str(p.start_ip))
            end = ipaddress.ip_address(str(p.end_ip))
        except ValueError:
            continue
        if start <= ip_addr <= end and p.pool_type == "dynamic":
            raise HTTPException(
                status_code=409,
                detail=f"IP {ip} falls inside dynamic pool {p.start_ip}-{p.end_ip}; exclude it first",
            )


@router.get("/scopes/{scope_id}/statics", response_model=list[StaticResponse])
async def list_statics(
    scope_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[DHCPStaticAssignment]:
    res = await db.execute(
        select(DHCPStaticAssignment).where(DHCPStaticAssignment.scope_id == scope_id)
    )
    return list(res.scalars().all())


@router.post(
    "/scopes/{scope_id}/statics",
    response_model=StaticResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_static(
    scope_id: uuid.UUID, body: StaticCreate, db: DB, user: SuperAdmin
) -> DHCPStaticAssignment:
    scope = await db.get(DHCPScope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    await _conflict_check(db, scope, body.ip_address, body.mac_address)
    st = DHCPStaticAssignment(
        scope_id=scope_id,
        created_by_user_id=user.id,
        **body.model_dump(),
    )
    db.add(st)
    await db.flush()
    await _upsert_ipam_for_static(db, scope, st)
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="dhcp_static_assignment",
        resource_id=str(st.id),
        resource_display=f"{body.mac_address}->{body.ip_address}",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(st)
    return st


@router.put("/statics/{static_id}", response_model=StaticResponse)
async def update_static(
    static_id: uuid.UUID, body: StaticUpdate, db: DB, user: SuperAdmin
) -> DHCPStaticAssignment:
    st = await db.get(DHCPStaticAssignment, static_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Static assignment not found")
    scope = await db.get(DHCPScope, st.scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    changes = body.model_dump(exclude_none=True)
    new_ip = changes.get("ip_address", str(st.ip_address))
    new_mac = changes.get("mac_address", str(st.mac_address))
    if "ip_address" in changes or "mac_address" in changes:
        await _conflict_check(db, scope, new_ip, new_mac, exclude_id=st.id)
    for k, v in changes.items():
        setattr(st, k, v)
    await db.flush()
    await _upsert_ipam_for_static(db, scope, st)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="dhcp_static_assignment",
        resource_id=str(st.id),
        resource_display=f"{st.mac_address}->{st.ip_address}",
        changed_fields=list(changes.keys()),
        new_value=body.model_dump(mode="json", exclude_none=True),
    )
    await db.commit()
    await db.refresh(st)
    return st


@router.delete("/statics/{static_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_static(static_id: uuid.UUID, db: DB, user: SuperAdmin) -> None:
    st = await db.get(DHCPStaticAssignment, static_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Static assignment not found")
    await _detach_ipam_for_static(db, st)
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="dhcp_static_assignment",
        resource_id=str(st.id),
        resource_display=f"{st.mac_address}->{st.ip_address}",
    )
    await db.delete(st)
    await db.commit()
