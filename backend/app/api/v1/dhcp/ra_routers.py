"""IPv6 Router-Advertisement management + rogue-RA API (issue #524).

Two operator surfaces, both group-scoped and gated by the
``ipv6.router_advertisements`` feature module (applied at the router include):

  * RA config preview — the rendered radvd.conf a group's RA-enabled scopes
    produce, plus a per-scope resolved-M/O summary, so operators can see what
    the DHCP agent will run before pushing.
  * Rogue-RA — the routers the agent's passive RA sniffer has seen, with an
    acknowledge action that allowlists a source so it stops classifying
    ``rogue``, plus direct allowlist CRUD.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser
from app.api.v1.dhcp._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.dhcp import (
    DHCPScope,
    DHCPServerGroup,
    RAObservedRouter,
    RARouterAllowlist,
)
from app.models.ipam import Subnet
from app.services.dhcp.radvd import build_ra_config, render_radvd_conf

router = APIRouter(
    tags=["dhcp"], dependencies=[Depends(require_resource_permission("dhcp_server"))]
)


# ── RA config preview ────────────────────────────────────────────────────────


class RAScopeConfig(BaseModel):
    scope_id: uuid.UUID
    subnet_id: uuid.UUID
    subnet_cidr: str
    interface: str
    managed_flag: bool
    other_flag: bool
    router_lifetime: int
    prefix_valid_lifetime: int
    prefix_preferred_lifetime: int
    prefix_on_link: bool
    prefix_autonomous: bool
    rdnss: list[str]
    dnssl: list[str]


class RAConfigPreview(BaseModel):
    group_id: uuid.UUID
    scopes: list[RAScopeConfig]
    radvd_conf: str


@router.get("/groups/{group_id}/ra-config", response_model=RAConfigPreview)
async def ra_config_preview(group_id: uuid.UUID, db: DB, _: CurrentUser) -> RAConfigPreview:
    """Preview the radvd.conf a group's RA-enabled scopes render to (#524)."""
    scope_rows = list(
        (
            await db.execute(
                select(DHCPScope)
                .where(
                    DHCPScope.group_id == group_id,
                    DHCPScope.is_active.is_(True),
                    DHCPScope.ra_enabled.is_(True),
                )
                .options(selectinload(DHCPScope.pools))
            )
        )
        .scalars()
        .all()
    )
    subnet_ids = [s.subnet_id for s in scope_rows]
    subnet_map: dict[uuid.UUID, Subnet] = {}
    if subnet_ids:
        for s in (
            (await db.execute(select(Subnet).where(Subnet.id.in_(subnet_ids)))).scalars().all()
        ):
            subnet_map[s.id] = s

    out: list[RAScopeConfig] = []
    ra_defs = []
    for sc in scope_rows:
        subnet = subnet_map.get(sc.subnet_id)
        if subnet is None:
            continue
        ra = build_ra_config(sc, subnet)
        if ra is None:
            continue
        ra_defs.append(ra)
        out.append(
            RAScopeConfig(
                scope_id=sc.id,
                subnet_id=sc.subnet_id,
                subnet_cidr=ra.subnet_cidr,
                interface=ra.interface,
                managed_flag=ra.managed_flag,
                other_flag=ra.other_flag,
                router_lifetime=ra.router_lifetime,
                prefix_valid_lifetime=ra.prefix_valid_lifetime,
                prefix_preferred_lifetime=ra.prefix_preferred_lifetime,
                prefix_on_link=ra.prefix_on_link,
                prefix_autonomous=ra.prefix_autonomous,
                rdnss=list(ra.rdnss),
                dnssl=list(ra.dnssl),
            )
        )
    return RAConfigPreview(group_id=group_id, scopes=out, radvd_conf=render_radvd_conf(ra_defs))


# ── Rogue-RA observed routers ────────────────────────────────────────────────


class ObservedRouterResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    source_ip: str
    source_mac: str | None
    prefixes: list[str]
    managed_flag: bool
    other_flag: bool
    router_lifetime: int | None
    iface: str | None
    classification: str
    first_seen_at: datetime
    last_seen_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("source_ip", "source_mac", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class AcknowledgeRequest(BaseModel):
    note: str = ""


@router.get("/groups/{group_id}/observed-routers", response_model=list[ObservedRouterResponse])
async def list_observed_routers(
    group_id: uuid.UUID, db: DB, _: CurrentUser, classification: str | None = None
) -> list[RAObservedRouter]:
    """List IPv6 routers observed advertising on this group's segments (#524)."""
    stmt = select(RAObservedRouter).where(RAObservedRouter.group_id == group_id)
    if classification:
        stmt = stmt.where(RAObservedRouter.classification == classification)
    stmt = stmt.order_by(RAObservedRouter.last_seen_at.desc())
    return list((await db.execute(stmt)).scalars().all())


@router.post(
    "/groups/{group_id}/observed-routers/{router_id}/acknowledge",
    response_model=ObservedRouterResponse,
)
async def acknowledge_observed_router(
    group_id: uuid.UUID,
    router_id: uuid.UUID,
    body: AcknowledgeRequest,
    db: DB,
    current_user: CurrentUser,
) -> RAObservedRouter:
    """Allowlist an RA source + reclassify it ``acknowledged`` (#524)."""
    row = await db.get(RAObservedRouter, router_id)
    if row is None or row.group_id != group_id:
        raise HTTPException(status_code=404, detail="Observed router not found")
    existing = (
        await db.execute(
            select(RARouterAllowlist).where(
                RARouterAllowlist.group_id == group_id,
                RARouterAllowlist.source_ip == row.source_ip,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            RARouterAllowlist(
                group_id=group_id,
                source_ip=str(row.source_ip),
                source_mac=str(row.source_mac) if row.source_mac else None,
                note=body.note,
                created_by_user_id=current_user.id,
            )
        )
    row.classification = "acknowledged"
    write_audit(
        db,
        user=current_user,
        action="acknowledge",
        resource_type="ra_router",
        resource_id=str(row.id),
        resource_display=str(row.source_ip),
        new_value={"note": body.note},
    )
    await db.commit()
    await db.refresh(row)
    return row


# ── RA allowlist CRUD ────────────────────────────────────────────────────────


class AllowlistResponse(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    source_ip: str | None
    source_mac: str | None
    note: str

    model_config = {"from_attributes": True}

    @field_validator("source_ip", "source_mac", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class AllowlistCreate(BaseModel):
    source_ip: str | None = None
    source_mac: str | None = None
    note: str = ""


@router.get("/groups/{group_id}/ra-allowlist", response_model=list[AllowlistResponse])
async def list_ra_allowlist(group_id: uuid.UUID, db: DB, _: CurrentUser) -> list[RARouterAllowlist]:
    """List a group's expected-RA-router allowlist (#524)."""
    return list(
        (
            await db.execute(
                select(RARouterAllowlist)
                .where(RARouterAllowlist.group_id == group_id)
                .order_by(RARouterAllowlist.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


@router.post("/groups/{group_id}/ra-allowlist", response_model=AllowlistResponse)
async def create_ra_allowlist(
    group_id: uuid.UUID, body: AllowlistCreate, db: DB, current_user: CurrentUser
) -> RARouterAllowlist:
    """Add an expected-RA-router allowlist entry (#524). Reclassifies matches."""
    grp = await db.get(DHCPServerGroup, group_id)
    if grp is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if not body.source_ip and not body.source_mac:
        raise HTTPException(status_code=422, detail="source_ip or source_mac is required")
    entry = RARouterAllowlist(
        group_id=group_id,
        source_ip=body.source_ip or None,
        source_mac=body.source_mac or None,
        note=body.note,
        created_by_user_id=current_user.id,
    )
    db.add(entry)
    # Reclassify any currently-rogue observation this entry now covers.
    if body.source_ip:
        rogue = (
            (
                await db.execute(
                    select(RAObservedRouter).where(
                        RAObservedRouter.group_id == group_id,
                        RAObservedRouter.source_ip == body.source_ip,
                        RAObservedRouter.classification == "rogue",
                    )
                )
            )
            .scalars()
            .all()
        )
        for r in rogue:
            r.classification = "acknowledged"
    write_audit(
        db,
        user=current_user,
        action="create",
        resource_type="ra_router_allowlist",
        resource_id=str(entry.id),
        resource_display=body.source_ip or body.source_mac or "",
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/groups/{group_id}/ra-allowlist/{entry_id}", status_code=204, response_model=None)
async def delete_ra_allowlist(
    group_id: uuid.UUID, entry_id: uuid.UUID, db: DB, current_user: CurrentUser
) -> None:
    """Remove an RA allowlist entry (#524)."""
    entry = await db.get(RARouterAllowlist, entry_id)
    if entry is None or entry.group_id != group_id:
        raise HTTPException(status_code=404, detail="Allowlist entry not found")
    write_audit(
        db,
        user=current_user,
        action="delete",
        resource_type="ra_router_allowlist",
        resource_id=str(entry.id),
        resource_display=str(entry.source_ip or entry.source_mac or ""),
    )
    await db.delete(entry)
    await db.commit()
