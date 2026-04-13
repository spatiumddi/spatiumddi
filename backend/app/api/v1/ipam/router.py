"""IPAM API — IP spaces, blocks, subnets, and addresses.

Phase 1 scaffold: spaces and subnets are fully wired; blocks and addresses
are stubbed with list/get/create/delete so the API surface exists while
full business logic is built out.
"""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DB
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────

class IPSpaceCreate(BaseModel):
    name: str
    description: str = ""
    is_default: bool = False
    tags: dict[str, Any] = {}


class IPSpaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    is_default: bool
    tags: dict[str, Any]

    model_config = {"from_attributes": True}


class SubnetCreate(BaseModel):
    space_id: uuid.UUID
    block_id: uuid.UUID | None = None
    network: str
    name: str = ""
    description: str = ""
    vlan_id: int | None = None
    vxlan_id: int | None = None
    gateway: str | None = None
    status: str = "active"
    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}


class SubnetResponse(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    block_id: uuid.UUID | None
    network: str
    name: str
    description: str
    vlan_id: int | None
    vxlan_id: int | None
    gateway: str | None
    status: str
    utilization_percent: float
    total_ips: int
    allocated_ips: int
    tags: dict[str, Any]
    custom_fields: dict[str, Any]

    model_config = {"from_attributes": True}


# ── IP Spaces ──────────────────────────────────────────────────────────────────

@router.get("/spaces", response_model=list[IPSpaceResponse])
async def list_spaces(current_user: CurrentUser, db: DB) -> list[IPSpace]:
    result = await db.execute(select(IPSpace).order_by(IPSpace.name))
    return list(result.scalars().all())


@router.post("/spaces", response_model=IPSpaceResponse, status_code=status.HTTP_201_CREATED)
async def create_space(body: IPSpaceCreate, current_user: CurrentUser, db: DB) -> IPSpace:
    space = IPSpace(**body.model_dump())
    db.add(space)

    from app.models.audit import AuditLog
    audit = AuditLog(
        user_id=current_user.id,
        user_display_name=current_user.display_name,
        auth_source=current_user.auth_source,
        action="create",
        resource_type="ip_space",
        resource_id="",  # filled after flush
        resource_display=body.name,
        new_value=body.model_dump(),
        result="success",
    )
    db.add(audit)
    await db.flush()

    audit.resource_id = str(space.id)
    await db.commit()
    await db.refresh(space)
    logger.info("ip_space_created", space_id=str(space.id), name=space.name)
    return space


@router.get("/spaces/{space_id}", response_model=IPSpaceResponse)
async def get_space(space_id: uuid.UUID, current_user: CurrentUser, db: DB) -> IPSpace:
    result = await db.execute(select(IPSpace).where(IPSpace.id == space_id))
    space = result.scalar_one_or_none()
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")
    return space


@router.delete("/spaces/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space(space_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    result = await db.execute(select(IPSpace).where(IPSpace.id == space_id))
    space = result.scalar_one_or_none()
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    from app.models.audit import AuditLog
    audit = AuditLog(
        user_id=current_user.id,
        user_display_name=current_user.display_name,
        auth_source=current_user.auth_source,
        action="delete",
        resource_type="ip_space",
        resource_id=str(space.id),
        resource_display=space.name,
        old_value={"name": space.name, "description": space.description},
        result="success",
    )
    db.add(audit)
    await db.delete(space)
    await db.commit()


# ── Subnets ────────────────────────────────────────────────────────────────────

@router.get("/subnets", response_model=list[SubnetResponse])
async def list_subnets(
    current_user: CurrentUser,
    db: DB,
    space_id: uuid.UUID | None = None,
) -> list[Subnet]:
    query = select(Subnet).order_by(Subnet.network)
    if space_id:
        query = query.where(Subnet.space_id == space_id)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/subnets", response_model=SubnetResponse, status_code=status.HTTP_201_CREATED)
async def create_subnet(body: SubnetCreate, current_user: CurrentUser, db: DB) -> Subnet:
    # Verify space exists
    space_result = await db.execute(select(IPSpace).where(IPSpace.id == body.space_id))
    if space_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    subnet = Subnet(**body.model_dump())
    db.add(subnet)

    from app.models.audit import AuditLog
    audit = AuditLog(
        user_id=current_user.id,
        user_display_name=current_user.display_name,
        auth_source=current_user.auth_source,
        action="create",
        resource_type="subnet",
        resource_id="",
        resource_display=f"{body.network} ({body.name})",
        new_value=body.model_dump(mode="json"),
        result="success",
    )
    db.add(audit)
    await db.flush()

    audit.resource_id = str(subnet.id)
    await db.commit()
    await db.refresh(subnet)
    logger.info("subnet_created", subnet_id=str(subnet.id), network=subnet.network)
    return subnet


@router.get("/subnets/{subnet_id}", response_model=SubnetResponse)
async def get_subnet(subnet_id: uuid.UUID, current_user: CurrentUser, db: DB) -> Subnet:
    result = await db.execute(select(Subnet).where(Subnet.id == subnet_id))
    subnet = result.scalar_one_or_none()
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")
    return subnet


@router.delete("/subnets/{subnet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subnet(subnet_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    result = await db.execute(select(Subnet).where(Subnet.id == subnet_id))
    subnet = result.scalar_one_or_none()
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    from app.models.audit import AuditLog
    audit = AuditLog(
        user_id=current_user.id,
        user_display_name=current_user.display_name,
        auth_source=current_user.auth_source,
        action="delete",
        resource_type="subnet",
        resource_id=str(subnet.id),
        resource_display=f"{subnet.network} ({subnet.name})",
        old_value={"network": subnet.network, "name": subnet.name},
        result="success",
    )
    db.add(audit)
    await db.delete(subnet)
    await db.commit()
