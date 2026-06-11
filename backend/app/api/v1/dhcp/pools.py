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
from app.core.agent_wake import collect_wake, dhcp_group_channel
from app.core.permissions import require_resource_permission
from app.models.dhcp import DHCPPool, DHCPScope
from app.models.ipam import IPAddress
from app.services.dhcp.windows_writethrough import push_pool_change

router = APIRouter(tags=["dhcp"], dependencies=[Depends(require_resource_permission("dhcp_pool"))])

# ``pd`` = DHCPv6 prefix-delegation pool (issue #368). For a pd pool the
# start_ip/end_ip range is ignored — the delegation is described by
# pd_prefix / delegated_length / excluded_prefix instead.
VALID_POOL_TYPES = {"dynamic", "excluded", "reserved", "pd"}


class PoolCreate(BaseModel):
    name: str = ""
    # Optional for pd pools (derived from pd_prefix); required for v4 ranges.
    start_ip: str | None = None
    end_ip: str | None = None
    pool_type: str = "dynamic"
    class_restriction: str | None = None
    lease_time_override: int | None = None
    options_override: dict[str, Any] | None = None
    # DHCPv6 prefix delegation (issue #368) — only for pool_type == "pd".
    pd_prefix: str | None = None
    delegated_length: int | None = None
    excluded_prefix: str | None = None

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
    pd_prefix: str | None = None
    delegated_length: int | None = None
    excluded_prefix: str | None = None


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
    pd_prefix: str | None = None
    delegated_length: int | None = None
    excluded_prefix: str | None = None
    existing_ips_in_range: list[dict[str, str]] | None = None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("start_ip", "end_ip", mode="before")
    @classmethod
    def _inet_to_str(cls, v: Any) -> Any:
        return str(v) if v is not None else v


def _ip_int(ip_str: str) -> int:
    # Family-agnostic so a v6 (address or pd) pool in the scope doesn't blow up
    # the overlap scan with an IPv4Address ValueError (#368).
    return int(ipaddress.ip_address(ip_str))


def _validate_pd(
    pd_prefix: str | None, delegated_length: int | None, excluded_prefix: str | None
) -> tuple[ipaddress.IPv6Network, str]:
    """Validate a DHCPv6 prefix-delegation pool (issue #368). Returns the
    parsed prefix network. 422 on any malformed input. Shared by create + update."""
    if not pd_prefix or not delegated_length:
        raise HTTPException(
            status_code=422,
            detail="pd pools require pd_prefix and delegated_length",
        )
    try:
        net = ipaddress.ip_network(pd_prefix, strict=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid pd_prefix: {exc}") from exc
    if not isinstance(net, ipaddress.IPv6Network):
        raise HTTPException(status_code=422, detail="pd_prefix must be an IPv6 prefix")
    dl = int(delegated_length)
    if dl < net.prefixlen or dl > 128:
        raise HTTPException(
            status_code=422,
            detail=(
                f"delegated_length {dl} must be between the pd_prefix length "
                f"{net.prefixlen} and 128"
            ),
        )
    if excluded_prefix:
        try:
            ex = ipaddress.ip_network(excluded_prefix, strict=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid excluded_prefix: {exc}") from exc
        if not isinstance(ex, ipaddress.IPv6Network) or not ex.subnet_of(net):
            raise HTTPException(
                status_code=422,
                detail="excluded_prefix must be an IPv6 sub-prefix of pd_prefix",
            )
        # RFC 6603: the excluded prefix is carved out of each delegated prefix,
        # so it must be strictly longer than the delegated length — Kea rejects
        # the config otherwise (#368 review).
        if ex.prefixlen <= dl:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"excluded_prefix length {ex.prefixlen} must be greater than "
                    f"delegated_length {dl}"
                ),
            )
    return net, str(net.network_address)


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
        # pd pools (#368) carry a prefix network address in start/end_ip as a
        # NOT-NULL placeholder, not an address range — they never overlap an
        # address pool, so skip them (also avoids comparing across families).
        if p.pool_type == "pd":
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

    if body.pool_type == "pd":
        # DHCPv6 prefix-delegation pool (issue #368). No v4 range / overlap
        # logic — validate the prefix + delegated length, then store the
        # prefix network address in start_ip/end_ip (NOT NULL placeholders).
        net, _start = _validate_pd(body.pd_prefix, body.delegated_length, body.excluded_prefix)
        values = body.model_dump()
        values["start_ip"] = str(net.network_address)
        values["end_ip"] = str(net.network_address)
        pool = DHCPPool(scope_id=scope_id, **values)
        db.add(pool)
        await db.flush()
        collect_wake(dhcp_group_channel(scope.group_id))
        write_audit(
            db,
            user=user,
            action="create",
            resource_type="dhcp_pool",
            resource_id=str(pool.id),
            resource_display=f"pd {body.pd_prefix} /{body.delegated_length}",
            new_value=body.model_dump(mode="json"),
        )
        await db.commit()
        await db.refresh(pool)
        return PoolResponse.model_validate(pool, from_attributes=True)

    if not body.start_ip or not body.end_ip:
        raise HTTPException(status_code=422, detail="start_ip and end_ip are required")
    overlap = await _check_pool_overlap(db, scope_id, body.start_ip, body.end_ip)
    if overlap:
        raise HTTPException(status_code=409, detail=overlap)
    pool = DHCPPool(scope_id=scope_id, **body.model_dump())
    db.add(pool)
    await db.flush()
    await push_pool_change(db, pool, action="create")
    collect_wake(dhcp_group_channel(scope.group_id))
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
    # Snapshot the old range BEFORE mutating, so we can remove the old
    # exclusion from Windows if it shifted.
    prev_start = str(pool.start_ip)
    prev_end = str(pool.end_ip)
    new_start = body.start_ip or prev_start
    new_end = body.end_ip or prev_end
    effective_type = body.pool_type if body.pool_type is not None else pool.pool_type
    if effective_type == "pd":
        # Re-validate the (merged) pd fields so a bad edit can't silently make a
        # working pd pool unrenderable (#368). Re-sync start/end placeholders to
        # the (possibly new) prefix network address.
        net, net_addr = _validate_pd(
            body.pd_prefix if body.pd_prefix is not None else pool.pd_prefix,
            body.delegated_length if body.delegated_length is not None else pool.delegated_length,
            body.excluded_prefix if body.excluded_prefix is not None else pool.excluded_prefix,
        )
        pool.start_ip = net_addr  # type: ignore[assignment]
        pool.end_ip = net_addr  # type: ignore[assignment]
    elif body.start_ip or body.end_ip:
        overlap = await _check_pool_overlap(
            db, pool.scope_id, new_start, new_end, exclude_id=pool.id
        )
        if overlap:
            raise HTTPException(status_code=409, detail=overlap)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(pool, k, v)
    await db.flush()
    await push_pool_change(db, pool, action="update", prev_start=prev_start, prev_end=prev_end)
    scope = await db.get(DHCPScope, pool.scope_id)
    if scope is not None:
        collect_wake(dhcp_group_channel(scope.group_id))
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
    scope = await db.get(DHCPScope, pool.scope_id)
    if scope is not None:
        collect_wake(dhcp_group_channel(scope.group_id))
    await push_pool_change(db, pool, action="delete")
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
