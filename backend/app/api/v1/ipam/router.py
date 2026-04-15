"""IPAM API — IP spaces, blocks, subnets, and addresses."""

import ipaddress
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DB
from app.api.v1.ipam.io_router import router as io_router
from app.models.audit import AuditLog
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

logger = structlog.get_logger(__name__)
router = APIRouter()
router.include_router(io_router)

# ── Internal helpers ───────────────────────────────────────────────────────────

def _parse_network(network: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Parse and validate a CIDR string. Raises ValueError on bad input."""
    try:
        return ipaddress.ip_network(network, strict=False)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid CIDR notation: {network}",
        )


def _total_ips(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> int:
    """Usable host count (excludes network/broadcast for prefixlen < 31)."""
    if net.prefixlen >= 31:
        return net.num_addresses
    return net.num_addresses - 2


async def _assert_no_overlap(
    db: AsyncSession,
    space_id: uuid.UUID,
    network: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Raise 409 if the given network overlaps with any existing subnet in the space."""
    q = (
        "SELECT network FROM subnet "
        "WHERE space_id = CAST(:space_id AS uuid) AND network && CAST(:network AS cidr)"
    )
    params: dict[str, Any] = {"space_id": str(space_id), "network": network}
    if exclude_id:
        q += " AND id != CAST(:exclude_id AS uuid)"
        params["exclude_id"] = str(exclude_id)
    result = await db.execute(text(q), params)
    row = result.fetchone()
    if row:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Network {network} overlaps with existing subnet {row[0]}",
        )


async def _update_utilization(db: AsyncSession, subnet_id: uuid.UUID) -> None:
    """Recompute and persist allocated_ips and utilization_percent for a subnet."""
    allocated = await db.scalar(
        select(func.count())
        .select_from(IPAddress)
        .where(IPAddress.subnet_id == subnet_id)
        .where(IPAddress.status != "available")
    ) or 0

    subnet = await db.get(Subnet, subnet_id)
    if subnet:
        subnet.allocated_ips = allocated
        subnet.utilization_percent = (
            round(allocated / subnet.total_ips * 100, 2) if subnet.total_ips > 0 else 0.0
        )


async def _update_block_utilization(db: AsyncSession, block_id: uuid.UUID) -> None:
    """Recompute utilization_percent for a block by summing allocated IPs across all
    descendant subnets (recursive), expressed as a fraction of the block's CIDR size.
    Also updates all ancestor blocks up the tree.
    """
    block = await db.get(IPBlock, block_id)
    if block is None:
        return

    # Sum allocated_ips for all subnets in this block and all descendant blocks
    result = await db.execute(
        text("""
            WITH RECURSIVE descendants AS (
                SELECT id FROM ip_block WHERE id = CAST(:block_id AS uuid)
                UNION ALL
                SELECT b.id FROM ip_block b
                    INNER JOIN descendants d ON b.parent_block_id = d.id
            )
            SELECT COALESCE(SUM(s.allocated_ips), 0)
            FROM subnet s
            WHERE s.block_id IN (SELECT id FROM descendants)
        """),
        {"block_id": str(block_id)},
    )
    allocated = result.scalar() or 0

    net = ipaddress.ip_network(str(block.network), strict=False)
    block_total = net.num_addresses
    block.utilization_percent = (
        round(float(allocated) / block_total * 100, 2) if block_total > 0 else 0.0
    )

    # Walk up the tree and update each ancestor
    if block.parent_block_id:
        await _update_block_utilization(db, block.parent_block_id)


def _audit(
    user: Any,
    action: str,
    resource_type: str,
    resource_id: str,
    resource_display: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> AuditLog:
    return AuditLog(
        user_id=user.id,
        user_display_name=user.display_name,
        auth_source=user.auth_source,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_display=resource_display,
        old_value=old_value,
        new_value=new_value,
        result="success",
    )


# ── Schemas ────────────────────────────────────────────────────────────────────

class IPSpaceCreate(BaseModel):
    name: str
    description: str = ""
    is_default: bool = False
    tags: dict[str, Any] = {}


class IPSpaceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_default: bool | None = None
    tags: dict[str, Any] | None = None


class IPSpaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    is_default: bool
    tags: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


class IPBlockCreate(BaseModel):
    space_id: uuid.UUID
    parent_block_id: uuid.UUID | None = None
    network: str
    name: str = ""
    description: str = ""
    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError:
            raise ValueError(f"Invalid CIDR notation: {v}")
        return v


class IPBlockUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None


class IPBlockResponse(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    parent_block_id: uuid.UUID | None
    network: str
    name: str
    description: str
    utilization_percent: float
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("network", mode="before")
    @classmethod
    def coerce_network(cls, v: Any) -> str:
        return str(v)


class SubnetCreate(BaseModel):
    space_id: uuid.UUID
    block_id: uuid.UUID
    network: str
    name: str = ""
    description: str = ""
    vlan_id: int | None = None
    vxlan_id: int | None = None
    gateway: str | None = None          # None → auto-assign first usable IP
    status: str = "active"
    skip_auto_addresses: bool = False   # True for loopbacks/P2P — skips network/broadcast/gateway records
    dns_servers: list[str] | None = None
    domain_name: str | None = None
    ntp_servers: list[str] | None = None
    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=True)
            return v
        except ValueError:
            pass
        # If strict fails, check whether host bits are the problem
        try:
            canonical = str(ipaddress.ip_network(v, strict=False))
            raise ValueError(f"Host bits are set in '{v}'. Did you mean {canonical}?")
        except ValueError as e:
            if "Did you mean" in str(e):
                raise
            raise ValueError(f"Invalid CIDR notation: {v}")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"active", "deprecated", "reserved", "quarantine"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v


class SubnetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    block_id: uuid.UUID | None = None
    vlan_id: int | None = None
    vxlan_id: int | None = None
    gateway: str | None = None
    status: str | None = None
    dns_servers: list[str] | None = None
    domain_name: str | None = None
    ntp_servers: list[str] | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None
    # When True: remove network/broadcast/gateway auto records.
    # When False: create them if not already present.
    manage_auto_addresses: bool | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"active", "deprecated", "reserved", "quarantine"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v


class SubnetResponse(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    block_id: uuid.UUID
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
    dns_servers: list[str] | None
    domain_name: str | None
    ntp_servers: list[str] | None
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("network", "gateway", mode="before")
    @classmethod
    def coerce_inet(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class IPAddressCreate(BaseModel):
    address: str
    status: str = "allocated"
    hostname: str
    mac_address: str | None = None
    description: str = ""
    owner_user_id: uuid.UUID | None = None
    custom_fields: dict[str, Any] = {}
    tags: dict[str, Any] = {}

    @field_validator("hostname")
    @classmethod
    def hostname_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Hostname is required")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"allocated", "reserved", "dhcp", "static_dhcp", "deprecated"}
        if v not in allowed:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(allowed))}. "
                "Use 'reserved' for gateway/infrastructure IPs."
            )
        return v


class IPAddressUpdate(BaseModel):
    status: str | None = None
    hostname: str | None = None
    mac_address: str | None = None
    description: str | None = None
    owner_user_id: uuid.UUID | None = None
    custom_fields: dict[str, Any] | None = None
    tags: dict[str, Any] | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"available", "allocated", "reserved", "static_dhcp", "deprecated"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v


class IPAddressResponse(BaseModel):
    id: uuid.UUID
    subnet_id: uuid.UUID
    address: str
    status: str
    hostname: str | None
    fqdn: str | None
    mac_address: str | None
    description: str
    owner_user_id: uuid.UUID | None
    last_seen_at: datetime | None
    last_seen_method: str | None
    custom_fields: dict[str, Any]
    tags: dict[str, Any]
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("address", "mac_address", mode="before")
    @classmethod
    def coerce_inet(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class NextIPRequest(BaseModel):
    strategy: str = "sequential"
    status: str = "allocated"
    hostname: str
    mac_address: str | None = None
    description: str = ""
    custom_fields: dict[str, Any] = {}
    tags: dict[str, Any] = {}

    @field_validator("hostname")
    @classmethod
    def hostname_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Hostname is required")
        return v

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        if v not in {"sequential", "random"}:
            raise ValueError("strategy must be 'sequential' or 'random'")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"allocated", "reserved", "dhcp", "static_dhcp"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v


# ── IP Spaces ──────────────────────────────────────────────────────────────────

@router.get("/spaces", response_model=list[IPSpaceResponse])
async def list_spaces(current_user: CurrentUser, db: DB) -> list[IPSpace]:
    result = await db.execute(select(IPSpace).order_by(IPSpace.name))
    return list(result.scalars().all())


@router.post("/spaces", response_model=IPSpaceResponse, status_code=status.HTTP_201_CREATED)
async def create_space(body: IPSpaceCreate, current_user: CurrentUser, db: DB) -> IPSpace:
    space = IPSpace(**body.model_dump())
    db.add(space)
    await db.flush()
    db.add(_audit(current_user, "create", "ip_space", str(space.id), body.name, new_value=body.model_dump()))
    await db.commit()
    await db.refresh(space)
    logger.info("ip_space_created", space_id=str(space.id), name=space.name)
    return space


@router.get("/spaces/{space_id}", response_model=IPSpaceResponse)
async def get_space(space_id: uuid.UUID, current_user: CurrentUser, db: DB) -> IPSpace:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")
    return space


@router.put("/spaces/{space_id}", response_model=IPSpaceResponse)
async def update_space(
    space_id: uuid.UUID, body: IPSpaceUpdate, current_user: CurrentUser, db: DB
) -> IPSpace:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    old = {"name": space.name, "description": space.description, "tags": space.tags}
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(space, field, value)

    db.add(_audit(current_user, "update", "ip_space", str(space.id), space.name, old_value=old, new_value=changes))
    await db.commit()
    await db.refresh(space)
    return space


@router.delete("/spaces/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space(space_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    db.add(_audit(current_user, "delete", "ip_space", str(space.id), space.name,
                  old_value={"name": space.name}))
    await db.delete(space)
    await db.commit()


# ── IP Blocks ──────────────────────────────────────────────────────────────────

@router.get("/blocks", response_model=list[IPBlockResponse])
async def list_blocks(
    current_user: CurrentUser,
    db: DB,
    space_id: uuid.UUID | None = None,
) -> list[IPBlock]:
    query = select(IPBlock).order_by(IPBlock.network)
    if space_id:
        query = query.where(IPBlock.space_id == space_id)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/blocks", response_model=IPBlockResponse, status_code=status.HTTP_201_CREATED)
async def create_block(body: IPBlockCreate, current_user: CurrentUser, db: DB) -> IPBlock:
    # Verify space exists
    if await db.get(IPSpace, body.space_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    # Verify parent block exists and belongs to the same space
    if body.parent_block_id:
        parent = await db.get(IPBlock, body.parent_block_id)
        if parent is None or parent.space_id != body.space_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent block not found in this space")
        # Validate child fits within parent
        child_net = _parse_network(body.network)
        parent_net = _parse_network(str(parent.network))
        if not child_net.subnet_of(parent_net):  # type: ignore[arg-type]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{body.network} is not contained within parent block {parent.network}",
            )

    block = IPBlock(**body.model_dump())
    db.add(block)
    await db.flush()
    db.add(_audit(current_user, "create", "ip_block", str(block.id),
                  f"{body.network} ({body.name})", new_value=body.model_dump(mode="json")))
    await db.commit()
    await db.refresh(block)
    logger.info("ip_block_created", block_id=str(block.id), network=block.network)
    return block


@router.get("/blocks/{block_id}", response_model=IPBlockResponse)
async def get_block(block_id: uuid.UUID, current_user: CurrentUser, db: DB) -> IPBlock:
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")
    return block


@router.put("/blocks/{block_id}", response_model=IPBlockResponse)
async def update_block(
    block_id: uuid.UUID, body: IPBlockUpdate, current_user: CurrentUser, db: DB
) -> IPBlock:
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")

    old = {"name": block.name, "description": block.description}
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(block, field, value)

    db.add(_audit(current_user, "update", "ip_block", str(block.id),
                  f"{block.network} ({block.name})", old_value=old, new_value=changes))
    await db.commit()
    await db.refresh(block)
    return block


@router.delete("/blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_block(block_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")

    db.add(_audit(current_user, "delete", "ip_block", str(block.id),
                  f"{block.network} ({block.name})", old_value={"network": str(block.network)}))
    await db.delete(block)
    await db.commit()


# ── Subnets ────────────────────────────────────────────────────────────────────

@router.get("/subnets", response_model=list[SubnetResponse])
async def list_subnets(
    current_user: CurrentUser,
    db: DB,
    space_id: uuid.UUID | None = None,
    block_id: uuid.UUID | None = None,
) -> list[Subnet]:
    query = select(Subnet).order_by(Subnet.network)
    if space_id:
        query = query.where(Subnet.space_id == space_id)
    if block_id:
        query = query.where(Subnet.block_id == block_id)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/subnets", response_model=SubnetResponse, status_code=status.HTTP_201_CREATED)
async def create_subnet(body: SubnetCreate, current_user: CurrentUser, db: DB) -> Subnet:
    if await db.get(IPSpace, body.space_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    block = await db.get(IPBlock, body.block_id)
    if block is None or block.space_id != body.space_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found in this space")

    net = _parse_network(body.network)
    canonical = str(net)  # normalise e.g. "10.0.0.1/24" → "10.0.0.0/24"

    await _assert_no_overlap(db, body.space_id, canonical)

    # Validate gateway is within the subnet if explicitly provided
    if body.gateway:
        try:
            gw = ipaddress.ip_address(body.gateway)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid gateway IP: {body.gateway}")
        if gw not in net:
            raise HTTPException(
                status_code=422,
                detail=f"Gateway {body.gateway} is not within subnet {canonical}",
            )

    total = _total_ips(net)

    subnet = Subnet(
        **{**body.model_dump(exclude={"skip_auto_addresses"}), "network": canonical},
        total_ips=total,
        utilization_percent=0.0,
        allocated_ips=0,
    )
    db.add(subnet)
    await db.flush()

    # For standard subnets (prefixlen < 31), create network, broadcast, and gateway records
    # unless skip_auto_addresses is set (e.g. loopbacks, point-to-point links).
    auto_created: list[str] = []
    if net.prefixlen < 31 and not body.skip_auto_addresses:
        # Network address (e.g. 10.0.1.0)
        db.add(IPAddress(
            subnet_id=subnet.id,
            address=str(net.network_address),
            status="network",
            description="Network address",
            created_by_user_id=current_user.id,
        ))
        auto_created.append(str(net.network_address))

        # Broadcast address (e.g. 10.0.1.255)
        db.add(IPAddress(
            subnet_id=subnet.id,
            address=str(net.broadcast_address),
            status="broadcast",
            description="Broadcast address",
            created_by_user_id=current_user.id,
        ))
        auto_created.append(str(net.broadcast_address))

        # Gateway — use provided or default to first usable host
        gw_addr = body.gateway or str(net.network_address + 1)
        db.add(IPAddress(
            subnet_id=subnet.id,
            address=gw_addr,
            status="reserved",
            description="Gateway",
            hostname="gateway",
            created_by_user_id=current_user.id,
        ))
        subnet.gateway = gw_addr
        auto_created.append(gw_addr)

    db.add(_audit(current_user, "create", "subnet", str(subnet.id),
                  f"{canonical} ({body.name})", new_value={**body.model_dump(mode="json"), "network": canonical}))
    await db.flush()

    if auto_created:
        await _update_utilization(db, subnet.id)

    await _update_block_utilization(db, subnet.block_id)
    await db.commit()
    await db.refresh(subnet)
    logger.info("subnet_created", subnet_id=str(subnet.id), network=canonical,
                gateway=subnet.gateway)
    return subnet


@router.get("/subnets/{subnet_id}", response_model=SubnetResponse)
async def get_subnet(subnet_id: uuid.UUID, current_user: CurrentUser, db: DB) -> Subnet:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")
    return subnet


@router.put("/subnets/{subnet_id}", response_model=SubnetResponse)
async def update_subnet(
    subnet_id: uuid.UUID, body: SubnetUpdate, current_user: CurrentUser, db: DB
) -> Subnet:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    # Validate new gateway is within the subnet
    if body.gateway is not None:
        try:
            gw = ipaddress.ip_address(body.gateway)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid gateway IP: {body.gateway}")
        net = _parse_network(str(subnet.network))
        if gw not in net:
            raise HTTPException(
                status_code=422,
                detail=f"Gateway {body.gateway} is not within subnet {subnet.network}",
            )

    old = {
        "name": subnet.name, "description": subnet.description,
        "gateway": str(subnet.gateway) if subnet.gateway else None,
        "status": subnet.status, "vlan_id": subnet.vlan_id,
    }
    changes = body.model_dump(exclude_none=True, exclude={"manage_auto_addresses"})
    for field, value in changes.items():
        setattr(subnet, field, value)

    # Handle add/remove of auto-created network/broadcast/gateway records
    if body.manage_auto_addresses is not None:
        net = _parse_network(str(subnet.network))
        if net.prefixlen < 31:
            auto_statuses = {"network", "broadcast"}
            existing_result = await db.execute(
                select(IPAddress).where(
                    IPAddress.subnet_id == subnet.id,
                    IPAddress.status.in_(auto_statuses),
                )
            )
            existing_auto = existing_result.scalars().all()

            if body.manage_auto_addresses is False:
                # Add: create records that are missing
                existing_addrs = {str(a.address) for a in existing_auto}
                if str(net.network_address) not in existing_addrs:
                    db.add(IPAddress(
                        subnet_id=subnet.id,
                        address=str(net.network_address),
                        status="network",
                        description="Network address",
                        created_by_user_id=current_user.id,
                    ))
                if str(net.broadcast_address) not in existing_addrs:
                    db.add(IPAddress(
                        subnet_id=subnet.id,
                        address=str(net.broadcast_address),
                        status="broadcast",
                        description="Broadcast address",
                        created_by_user_id=current_user.id,
                    ))
                await db.flush()
                await _update_utilization(db, subnet.id)
            else:
                # Remove: permanently delete network/broadcast records
                for addr in existing_auto:
                    await db.delete(addr)
                await db.flush()
                await _update_utilization(db, subnet.id)

    db.add(_audit(current_user, "update", "subnet", str(subnet.id),
                  f"{subnet.network} ({subnet.name})", old_value=old, new_value=changes))
    await db.commit()
    await db.refresh(subnet)
    return subnet


@router.delete("/subnets/{subnet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subnet(subnet_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    block_id = subnet.block_id
    db.add(_audit(current_user, "delete", "subnet", str(subnet.id),
                  f"{subnet.network} ({subnet.name})",
                  old_value={"network": str(subnet.network), "name": subnet.name}))
    await db.delete(subnet)
    await db.flush()
    await _update_block_utilization(db, block_id)
    await db.commit()


# ── IP Addresses ───────────────────────────────────────────────────────────────

@router.get("/subnets/{subnet_id}/addresses", response_model=list[IPAddressResponse])
async def list_addresses(
    subnet_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    status_filter: str | None = None,
) -> list[IPAddress]:
    if await db.get(Subnet, subnet_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    query = select(IPAddress).where(IPAddress.subnet_id == subnet_id).order_by(
        text("CAST(address AS inet)")
    )
    if status_filter:
        query = query.where(IPAddress.status == status_filter)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post(
    "/subnets/{subnet_id}/addresses",
    response_model=IPAddressResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_address(
    subnet_id: uuid.UUID, body: IPAddressCreate, current_user: CurrentUser, db: DB
) -> IPAddress:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    # Validate address belongs to subnet
    try:
        addr = ipaddress.ip_address(body.address)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid IP address: {body.address}")

    net = _parse_network(str(subnet.network))
    if addr not in net:
        raise HTTPException(
            status_code=422,
            detail=f"Address {body.address} is not within subnet {subnet.network}",
        )

    # MAC address required for static_dhcp
    if body.status == "static_dhcp" and not body.mac_address:
        raise HTTPException(
            status_code=422,
            detail="mac_address is required when status is 'static_dhcp'",
        )

    # Check address not already in use
    existing = await db.scalar(
        select(func.count()).select_from(IPAddress)
        .where(IPAddress.subnet_id == subnet_id)
        .where(IPAddress.address == body.address)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Address {body.address} is already allocated in this subnet",
        )

    ip = IPAddress(
        subnet_id=subnet_id,
        created_by_user_id=current_user.id,
        **body.model_dump(),
    )
    db.add(ip)
    await db.flush()

    db.add(_audit(current_user, "create", "ip_address", str(ip.id),
                  body.address, new_value=body.model_dump()))
    await db.flush()
    await _update_utilization(db, subnet_id)
    await _update_block_utilization(db, subnet.block_id)
    await db.commit()
    await db.refresh(ip)
    logger.info("ip_address_created", ip_id=str(ip.id), address=body.address,
                subnet_id=str(subnet_id))
    return ip


@router.get("/addresses/{address_id}", response_model=IPAddressResponse)
async def get_address(address_id: uuid.UUID, current_user: CurrentUser, db: DB) -> IPAddress:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP address not found")
    return ip


@router.put("/addresses/{address_id}", response_model=IPAddressResponse)
async def update_address(
    address_id: uuid.UUID, body: IPAddressUpdate, current_user: CurrentUser, db: DB
) -> IPAddress:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP address not found")

    # MAC required if transitioning to static_dhcp
    new_status = body.status or ip.status
    new_mac = body.mac_address if body.mac_address is not None else ip.mac_address
    if new_status == "static_dhcp" and not new_mac:
        raise HTTPException(
            status_code=422,
            detail="mac_address is required when status is 'static_dhcp'",
        )

    old = {"status": ip.status, "hostname": ip.hostname, "mac_address": str(ip.mac_address) if ip.mac_address else None}
    old_status = ip.status
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(ip, field, value)

    db.add(_audit(current_user, "update", "ip_address", str(ip.id),
                  str(ip.address), old_value=old, new_value=changes))

    # Update utilization if status changed (available ↔ non-available)
    status_was_available = old_status == "available"
    status_now_available = ip.status == "available"
    if status_was_available != status_now_available:
        await db.flush()
        subnet = await db.get(Subnet, ip.subnet_id)
        await _update_utilization(db, ip.subnet_id)
        if subnet:
            await _update_block_utilization(db, subnet.block_id)

    await db.commit()
    await db.refresh(ip)
    return ip


@router.delete("/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(
    address_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    permanent: bool = Query(default=False, description="Permanently delete instead of marking orphan"),
) -> None:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP address not found")

    subnet = await db.get(Subnet, ip.subnet_id)
    if permanent:
        subnet_id = ip.subnet_id
        db.add(_audit(current_user, "delete", "ip_address", str(ip.id),
                      str(ip.address), old_value={"address": str(ip.address), "status": ip.status}))
        await db.delete(ip)
        await db.flush()
        await _update_utilization(db, subnet_id)
        if subnet:
            await _update_block_utilization(db, subnet.block_id)
    else:
        # Soft-delete: mark as orphan, keep the record
        old_status = ip.status
        ip.status = "orphan"
        db.add(_audit(current_user, "update", "ip_address", str(ip.id),
                      str(ip.address), old_value={"status": old_status}, new_value={"status": "orphan"}))
        await db.flush()
        await _update_utilization(db, ip.subnet_id)
        if subnet:
            await _update_block_utilization(db, subnet.block_id)
    await db.commit()


# ── Next available IP ──────────────────────────────────────────────────────────

@router.post(
    "/subnets/{subnet_id}/next",
    response_model=IPAddressResponse,
    status_code=status.HTTP_201_CREATED,
)
async def allocate_next_ip(
    subnet_id: uuid.UUID, body: NextIPRequest, current_user: CurrentUser, db: DB
) -> IPAddress:
    """Atomically allocate the next available IP in the subnet."""
    # Lock the subnet row to serialise concurrent allocations
    result = await db.execute(
        select(Subnet).where(Subnet.id == subnet_id).with_for_update()
    )
    subnet = result.scalar_one_or_none()
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    if body.status == "static_dhcp" and not body.mac_address:
        raise HTTPException(
            status_code=422,
            detail="mac_address is required when status is 'static_dhcp'",
        )

    net = _parse_network(str(subnet.network))

    # Fetch all used addresses in this subnet
    used_result = await db.execute(
        select(IPAddress.address).where(IPAddress.subnet_id == subnet_id)
    )
    # Normalise to string set; asyncpg returns INET as str
    used: set[str] = {str(row[0]) for row in used_result}

    # For large subnets, cap the search at first 65536 hosts
    MAX_SEARCH = 65536
    hosts = list(net.hosts()) if net.prefixlen >= 16 else list(net.hosts())[:MAX_SEARCH]

    if body.strategy == "random":
        import random
        random.shuffle(hosts)

    chosen: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    for host in hosts:
        if str(host) not in used:
            chosen = host
            break

    if chosen is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No available IP addresses in this subnet",
        )

    ip = IPAddress(
        subnet_id=subnet_id,
        address=str(chosen),
        status=body.status,
        hostname=body.hostname,
        mac_address=body.mac_address,
        description=body.description,
        custom_fields=body.custom_fields,
        tags=body.tags,
        created_by_user_id=current_user.id,
    )
    db.add(ip)
    await db.flush()

    db.add(_audit(current_user, "create", "ip_address", str(ip.id),
                  str(chosen), new_value={**body.model_dump(), "address": str(chosen)}))
    await db.flush()
    await _update_utilization(db, subnet_id)
    await _update_block_utilization(db, subnet.block_id)
    await db.commit()
    await db.refresh(ip)
    logger.info("ip_allocated", ip_id=str(ip.id), address=str(chosen),
                subnet_id=str(subnet_id), strategy=body.strategy)
    return ip
