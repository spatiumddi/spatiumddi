"""New-device (arpwatch) detection — operator REST surface (issue #459).

Review queue + allowlist + baseline import + one-click acknowledge / block over
the ``ip_mac_history`` classification layer. Gated behind the
``security.new_device_watch`` feature module (applied at the router include in
``app.api.v1.router``). Reads need ``read,ip_address``; mutations need
``write,ip_address``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB
from app.api.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.api.v1.dhcp._audit import write_audit
from app.core.agent_wake import collect_wake, dhcp_group_channel
from app.core.permissions import require_permission
from app.models.auth import User
from app.models.dhcp import DHCPMACBlock, DHCPServerGroup
from app.models.ipam import IPAddress, IpMacHistory, MACAllowlist, Subnet
from app.services.ipam.new_device import (
    BUILTIN_VIRT_OUIS,
    acknowledge_sighting,
    add_allowlist_entry,
    baseline_import,
    new_device_counts,
    normalize_oui_prefix,
    remove_allowlist_entry,
)
from app.services.oui import bulk_lookup_vendors, normalize_mac_key

router = APIRouter(prefix="/new-devices", tags=["new-devices"])

ReadUser = Annotated[User, Depends(require_permission("read", "ip_address"))]
WriteUser = Annotated[User, Depends(require_permission("write", "ip_address"))]


# ── Schemas ─────────────────────────────────────────────────────────────────


class SightingOut(BaseModel):
    id: uuid.UUID
    ip_address_id: uuid.UUID
    ip_address: str
    subnet_id: uuid.UUID | None
    subnet_name: str | None
    mac_address: str
    oui_vendor: str | None
    classification: str
    source: str
    is_randomized: bool
    first_seen: datetime
    last_seen: datetime
    acknowledged_at: datetime | None


class SummaryOut(BaseModel):
    new_count: int
    new_randomized_count: int
    new_last_24h: int
    acknowledged_count: int
    known_count: int
    allowlist_count: int


class AcknowledgeBody(BaseModel):
    note: str = ""


class BlockBody(BaseModel):
    mac_address: str
    group_id: uuid.UUID | None = None  # None → block in every DHCP server group
    reason: str = "other"
    description: str = ""


class BlockResult(BaseModel):
    mac_address: str
    blocked_group_ids: list[uuid.UUID]
    already_blocked_group_ids: list[uuid.UUID]


class AllowlistOut(BaseModel):
    id: uuid.UUID
    mac_address: str | None
    oui_prefix: str | None
    note: str
    is_builtin: bool
    created_at: datetime


class AllowlistCreate(BaseModel):
    mac_address: str | None = None
    oui_prefix: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _one_key(self) -> AllowlistCreate:
        if not self.mac_address and not self.oui_prefix:
            raise ValueError("provide a mac_address or an oui_prefix")
        return self


class AllowlistCreateResult(BaseModel):
    entry: AllowlistOut
    reclassified_count: int


class BaselineResult(BaseModel):
    reclassified_count: int


class VirtDefaultsResult(BaseModel):
    added: int
    skipped: int


def _sighting_out(
    h: IpMacHistory, ip: IPAddress, subnet: Subnet | None, vendors: dict[str, str]
) -> SightingOut:
    """Project a ``(IpMacHistory, IPAddress, Subnet)`` row tuple → ``SightingOut``.

    Single source of truth for the wire shape so the list + single-row endpoints
    can't drift (vendors is the bulk_lookup_vendors result for the page).
    """
    return SightingOut(
        id=h.id,
        ip_address_id=h.ip_address_id,
        ip_address=str(ip.address),
        subnet_id=subnet.id if subnet else None,
        subnet_name=subnet.name if subnet else None,
        mac_address=str(h.mac_address),
        oui_vendor=vendors.get(normalize_mac_key(str(h.mac_address)) or ""),
        classification=h.classification,
        source=h.source,
        is_randomized=h.is_randomized,
        first_seen=h.first_seen,
        last_seen=h.last_seen,
        acknowledged_at=h.acknowledged_at,
    )


# ── Reads ───────────────────────────────────────────────────────────────────


@router.get("/summary", response_model=SummaryOut)
async def get_summary(db: DB, _: ReadUser) -> SummaryOut:
    """Counts for the dashboard KPI + review-queue tabs."""
    c = await new_device_counts(db)
    return SummaryOut(
        new_count=c["new"],
        new_randomized_count=c["new_randomized"],
        new_last_24h=c["new_last_24h"],
        acknowledged_count=c["acknowledged"],
        known_count=c["known"],
        allowlist_count=c["allowlist"],
    )


@router.get("/sightings", response_model=Page[SightingOut])
async def list_sightings(
    db: DB,
    _: ReadUser,
    classification: str | None = Query("new"),
    subnet_id: uuid.UUID | None = None,
    since_hours: int | None = Query(None, ge=1, le=24 * 365),
    include_randomized: bool = False,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> Page[SightingOut]:
    """Paginated review queue. Defaults to unacknowledged new devices."""
    conds: list[Any] = []
    if classification:
        if classification not in ("new", "acknowledged", "known"):
            raise HTTPException(status_code=422, detail="invalid classification")
        conds.append(IpMacHistory.classification == classification)
    if subnet_id is not None:
        conds.append(IPAddress.subnet_id == subnet_id)
    if since_hours is not None:
        conds.append(IpMacHistory.first_seen >= datetime.now(UTC) - timedelta(hours=since_hours))
    if not include_randomized:
        conds.append(IpMacHistory.is_randomized.is_(False))
    if search:
        like = f"%{search.strip()}%"
        conds.append(
            or_(
                cast(IpMacHistory.mac_address, String).ilike(like),
                cast(IPAddress.address, String).ilike(like),
                IPAddress.hostname.ilike(like),
            )
        )

    base = (
        select(IpMacHistory, IPAddress, Subnet)
        .join(IPAddress, IPAddress.id == IpMacHistory.ip_address_id)
        .outerjoin(Subnet, Subnet.id == IPAddress.subnet_id)
        .where(and_(*conds))
    )
    total = (
        await db.execute(select(func.count()).select_from(base.order_by(None).subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(IpMacHistory.first_seen.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    macs: list[str | None] = [str(h.mac_address) for h, _ip, _s in rows]
    vendors = await bulk_lookup_vendors(db, macs)
    items = [_sighting_out(h, ip, subnet, vendors) for h, ip, subnet in rows]
    return Page[SightingOut](items=items, total=total, page=page, page_size=page_size)


@router.get("/allowlist", response_model=list[AllowlistOut])
async def list_allowlist(db: DB, _: ReadUser) -> list[AllowlistOut]:
    rows = (
        (await db.execute(select(MACAllowlist).order_by(MACAllowlist.created_at.desc())))
        .scalars()
        .all()
    )
    return [
        AllowlistOut(
            id=r.id,
            mac_address=str(r.mac_address) if r.mac_address else None,
            oui_prefix=r.oui_prefix,
            note=r.note,
            is_builtin=r.is_builtin,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ── Mutations ───────────────────────────────────────────────────────────────


async def _load_sighting_out(db: DB, sighting_id: uuid.UUID) -> SightingOut:
    row = (
        await db.execute(
            select(IpMacHistory, IPAddress, Subnet)
            .join(IPAddress, IPAddress.id == IpMacHistory.ip_address_id)
            .outerjoin(Subnet, Subnet.id == IPAddress.subnet_id)
            .where(IpMacHistory.id == sighting_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="sighting not found")
    h, ip, subnet = row
    one_mac: list[str | None] = [str(h.mac_address)]
    vendors = await bulk_lookup_vendors(db, one_mac)
    return _sighting_out(h, ip, subnet, vendors)


@router.post("/sightings/{sighting_id}/acknowledge", response_model=SightingOut)
async def acknowledge(
    sighting_id: uuid.UUID, body: AcknowledgeBody, db: DB, user: WriteUser
) -> SightingOut:
    """Dismiss a new-device sighting → ``acknowledged`` (fires device.acknowledged)."""
    row = await acknowledge_sighting(db, sighting_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="sighting not found")
    write_audit(
        db,
        user=user,
        action="acknowledged",
        resource_type="ip_mac_observation",
        resource_id=f"{row.ip_address_id}:{row.mac_address}",
        resource_display=str(row.mac_address),
        new_value={"mac_address": str(row.mac_address), "note": body.note},
    )
    await db.commit()
    return await _load_sighting_out(db, sighting_id)


@router.post("/baseline", response_model=BaselineResult)
async def run_baseline(db: DB, user: WriteUser) -> BaselineResult:
    """Mark every currently-observed MAC as ``known`` (learning-mode baseline)."""
    count = await baseline_import(db)
    write_audit(
        db,
        user=user,
        action="baseline_import",
        resource_type="ip_mac_observation",
        resource_id="*",
        resource_display="new-device baseline import",
        new_value={"reclassified_count": count},
    )
    await db.commit()
    return BaselineResult(reclassified_count=count)


@router.post(
    "/allowlist", response_model=AllowlistCreateResult, status_code=status.HTTP_201_CREATED
)
async def create_allowlist(body: AllowlistCreate, db: DB, user: WriteUser) -> AllowlistCreateResult:
    try:
        row, reclassified = await add_allowlist_entry(
            db,
            mac_address=body.mac_address,
            oui_prefix=body.oui_prefix,
            note=body.note,
            user=user,
        )
        await db.flush()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        # Already allowlisted (uq_mac_allowlist_mac / uq_mac_allowlist_oui_prefix).
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="That MAC or OUI prefix is already allowlisted"
        ) from exc
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="mac_allowlist",
        resource_id=str(row.id),
        resource_display=str(row.mac_address or row.oui_prefix),
        new_value={
            "mac_address": str(row.mac_address) if row.mac_address else None,
            "oui_prefix": row.oui_prefix,
            "reclassified_count": reclassified,
        },
    )
    await db.commit()
    await db.refresh(row)
    return AllowlistCreateResult(
        entry=AllowlistOut(
            id=row.id,
            mac_address=str(row.mac_address) if row.mac_address else None,
            oui_prefix=row.oui_prefix,
            note=row.note,
            is_builtin=row.is_builtin,
            created_at=row.created_at,
        ),
        reclassified_count=reclassified,
    )


@router.post("/allowlist/virt-defaults", response_model=VirtDefaultsResult)
async def add_virt_defaults(db: DB, user: WriteUser) -> VirtDefaultsResult:
    """Add the well-known virtualisation / container OUIs to the allowlist so VM
    and container MACs stop reading as rogue devices. Skips any already present."""
    existing = {
        r[0]
        for r in (
            await db.execute(
                select(MACAllowlist.oui_prefix).where(MACAllowlist.oui_prefix.is_not(None))
            )
        ).all()
    }
    added = 0
    for prefix, vendor in BUILTIN_VIRT_OUIS:
        if prefix in existing:
            continue
        await add_allowlist_entry(
            db, oui_prefix=prefix, note=f"{vendor} (built-in)", user=user, is_builtin=True
        )
        added += 1
    if added:
        write_audit(
            db,
            user=user,
            action="create",
            resource_type="mac_allowlist",
            resource_id="*",
            resource_display="virtualization OUI defaults",
            new_value={"added": added},
        )
    await db.commit()
    return VirtDefaultsResult(added=added, skipped=len(BUILTIN_VIRT_OUIS) - added)


@router.delete("/allowlist/{allowlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_allowlist(allowlist_id: uuid.UUID, db: DB, user: WriteUser) -> None:
    existing = await db.get(MACAllowlist, allowlist_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="allowlist entry not found")
    display = str(existing.mac_address or existing.oui_prefix)
    await remove_allowlist_entry(db, allowlist_id)
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="mac_allowlist",
        resource_id=str(allowlist_id),
        resource_display=display,
    )
    await db.commit()


@router.post("/block", response_model=BlockResult)
async def block_mac(body: BlockBody, db: DB, user: WriteUser) -> BlockResult:
    """Block a MAC in one DHCP server group (or all groups when ``group_id`` is
    omitted) — arpwatch with teeth. Creates ``dhcp_mac_block`` rows + wakes the
    affected agents."""
    if body.group_id is not None:
        group_ids = [body.group_id]
        if await db.get(DHCPServerGroup, body.group_id) is None:
            raise HTTPException(status_code=404, detail="DHCP server group not found")
    else:
        group_ids = list((await db.execute(select(DHCPServerGroup.id))).scalars().all())
        if not group_ids:
            raise HTTPException(status_code=409, detail="no DHCP server groups to block in")

    already = {
        r[0]
        for r in (
            await db.execute(
                select(DHCPMACBlock.group_id).where(
                    DHCPMACBlock.mac_address == body.mac_address,
                    DHCPMACBlock.group_id.in_(group_ids),
                )
            )
        ).all()
    }
    blocked: list[uuid.UUID] = []
    for gid in group_ids:
        if gid in already:
            continue
        db.add(
            DHCPMACBlock(
                group_id=gid,
                mac_address=body.mac_address,
                reason=body.reason,
                description=body.description or "Blocked from new-device review (#459)",
                enabled=True,
                created_by_user_id=user.id,
                updated_by_user_id=user.id,
            )
        )
        collect_wake(dhcp_group_channel(gid))
        blocked.append(gid)

    if blocked:
        write_audit(
            db,
            user=user,
            action="create",
            resource_type="dhcp_mac_block",
            resource_id=body.mac_address,
            resource_display=body.mac_address,
            new_value={"mac_address": body.mac_address, "blocked_groups": len(blocked)},
        )
    await db.commit()
    return BlockResult(
        mac_address=body.mac_address,
        blocked_group_ids=blocked,
        already_blocked_group_ids=list(already),
    )


# Keep ``normalize_oui_prefix`` reachable for tests that exercise the parser.
__all__ = ["router", "normalize_oui_prefix"]
