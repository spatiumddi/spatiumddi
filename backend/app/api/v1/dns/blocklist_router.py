"""DNS Blocking List (RPZ) API.

Manages:
  - /blocklists                    (CRUD)
  - /blocklists/{id}/entries       (CRUD + bulk-add)
  - /blocklists/{id}/exceptions    (CRUD)
  - /blocklists/{id}/refresh       (enqueue feed sync)
  - /blocklists/{id}/assignments   (attach to views / server groups)
  - /blocklists/effective/view/{view_id}
  - /blocklists/effective/group/{group_id}

All mutations are audited. No backend-specific logic (BIND9 RPZ)
lives in this module — the driver will consume the effective representation
produced by `app.services.dns_blocklist`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.models.audit import AuditLog
from app.models.dns import (
    DNSBlockList,
    DNSBlockListEntry,
    DNSBlockListException,
    DNSServerGroup,
    DNSView,
)
from app.services.dns_blocklist import (
    build_effective_for_group,
    build_effective_for_view,
    dedupe_domains,
)

logger = structlog.get_logger(__name__)
router = APIRouter()


VALID_SOURCE_TYPES = {"manual", "url", "file_upload"}
VALID_FEED_FORMATS = {"hosts", "domains", "adblock"}
VALID_BLOCK_MODES = {"nxdomain", "sinkhole", "refused"}
VALID_ENTRY_TYPES = {"block", "redirect", "nxdomain"}


# ── Schemas ─────────────────────────────────────────────────────────────────


class BlockListCreate(BaseModel):
    name: str
    description: str = ""
    category: str = "custom"
    source_type: str = "manual"
    feed_url: str | None = None
    feed_format: str = "hosts"
    update_interval_hours: int = 24
    block_mode: str = "nxdomain"
    sinkhole_ip: str | None = None
    enabled: bool = True

    @field_validator("source_type")
    @classmethod
    def _v_source(cls, v: str) -> str:
        if v not in VALID_SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}")
        return v

    @field_validator("feed_format")
    @classmethod
    def _v_format(cls, v: str) -> str:
        if v not in VALID_FEED_FORMATS:
            raise ValueError(f"feed_format must be one of {sorted(VALID_FEED_FORMATS)}")
        return v

    @field_validator("block_mode")
    @classmethod
    def _v_mode(cls, v: str) -> str:
        if v not in VALID_BLOCK_MODES:
            raise ValueError(f"block_mode must be one of {sorted(VALID_BLOCK_MODES)}")
        return v


class BlockListUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    source_type: str | None = None
    feed_url: str | None = None
    feed_format: str | None = None
    update_interval_hours: int | None = None
    block_mode: str | None = None
    sinkhole_ip: str | None = None
    enabled: bool | None = None

    @field_validator("source_type")
    @classmethod
    def _v_source(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}")
        return v

    @field_validator("feed_format")
    @classmethod
    def _v_format(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_FEED_FORMATS:
            raise ValueError(f"feed_format must be one of {sorted(VALID_FEED_FORMATS)}")
        return v

    @field_validator("block_mode")
    @classmethod
    def _v_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_BLOCK_MODES:
            raise ValueError(f"block_mode must be one of {sorted(VALID_BLOCK_MODES)}")
        return v


class BlockListResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    category: str
    source_type: str
    feed_url: str | None
    feed_format: str
    update_interval_hours: int
    block_mode: str
    sinkhole_ip: str | None
    enabled: bool
    last_synced_at: datetime | None
    last_sync_status: str | None
    last_sync_error: str | None
    entry_count: int
    created_at: datetime
    modified_at: datetime
    applied_group_ids: list[uuid.UUID] = []
    applied_view_ids: list[uuid.UUID] = []

    model_config = {"from_attributes": True}


class EntryCreate(BaseModel):
    domain: str
    entry_type: str = "block"
    target: str | None = None
    is_wildcard: bool = False

    @field_validator("entry_type")
    @classmethod
    def _v_et(cls, v: str) -> str:
        if v not in VALID_ENTRY_TYPES:
            raise ValueError(f"entry_type must be one of {sorted(VALID_ENTRY_TYPES)}")
        return v


class EntryUpdate(BaseModel):
    domain: str | None = None
    entry_type: str | None = None
    target: str | None = None
    is_wildcard: bool | None = None

    @field_validator("entry_type")
    @classmethod
    def _v_et_upd(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_ENTRY_TYPES:
            raise ValueError(f"entry_type must be one of {sorted(VALID_ENTRY_TYPES)}")
        return v


class EntryResponse(BaseModel):
    id: uuid.UUID
    list_id: uuid.UUID
    domain: str
    entry_type: str
    target: str | None
    source: str
    is_wildcard: bool
    added_at: datetime

    model_config = {"from_attributes": True}


class EntryPage(BaseModel):
    total: int
    items: list[EntryResponse]


class BulkAddRequest(BaseModel):
    domains: list[str]
    entry_type: str = "block"
    target: str | None = None
    is_wildcard: bool = False

    @field_validator("entry_type")
    @classmethod
    def _v_et(cls, v: str) -> str:
        if v not in VALID_ENTRY_TYPES:
            raise ValueError(f"entry_type must be one of {sorted(VALID_ENTRY_TYPES)}")
        return v


class BulkAddResponse(BaseModel):
    added: int
    skipped: int
    total: int


class ExceptionCreate(BaseModel):
    domain: str
    reason: str = ""


class ExceptionUpdate(BaseModel):
    domain: str | None = None
    reason: str | None = None


class ExceptionResponse(BaseModel):
    id: uuid.UUID
    list_id: uuid.UUID
    domain: str
    reason: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AssignmentUpdate(BaseModel):
    server_group_ids: list[uuid.UUID] | None = None
    view_ids: list[uuid.UUID] | None = None


class RefreshResponse(BaseModel):
    list_id: uuid.UUID
    task_id: str | None
    status: str


class EffectiveEntryResponse(BaseModel):
    domain: str
    action: str
    block_mode: str
    sinkhole_ip: str | None
    target: str | None
    is_wildcard: bool
    list_id: uuid.UUID
    list_name: str


class EffectiveBlocklistResponse(BaseModel):
    scope: str
    scope_id: uuid.UUID
    entries: list[EffectiveEntryResponse]
    exceptions: list[str]
    lists: list[uuid.UUID]


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _require_list(list_id: uuid.UUID, db: DB) -> DNSBlockList:
    result = await db.execute(
        select(DNSBlockList)
        .where(DNSBlockList.id == list_id)
        .options(
            selectinload(DNSBlockList.server_groups),
            selectinload(DNSBlockList.views),
        )
    )
    bl = result.scalar_one_or_none()
    if bl is None:
        raise HTTPException(status_code=404, detail="Blocklist not found")
    return bl


def _to_response(bl: DNSBlockList) -> BlockListResponse:
    return BlockListResponse(
        id=bl.id,
        name=bl.name,
        description=bl.description,
        category=bl.category,
        source_type=bl.source_type,
        feed_url=bl.feed_url,
        feed_format=bl.feed_format,
        update_interval_hours=bl.update_interval_hours,
        block_mode=bl.block_mode,
        sinkhole_ip=bl.sinkhole_ip,
        enabled=bl.enabled,
        last_synced_at=bl.last_synced_at,
        last_sync_status=bl.last_sync_status,
        last_sync_error=bl.last_sync_error,
        entry_count=bl.entry_count,
        created_at=bl.created_at,
        modified_at=bl.modified_at,
        applied_group_ids=[g.id for g in bl.server_groups],
        applied_view_ids=[v.id for v in bl.views],
    )


def _audit(
    current_user: Any,
    action: str,
    resource_type: str,
    resource_id: str,
    resource_display: str,
    changed_fields: list[str] | None = None,
) -> AuditLog:
    return AuditLog(
        user_id=current_user.id,
        user_display_name=current_user.display_name,
        auth_source=current_user.auth_source,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_display=resource_display,
        changed_fields=changed_fields,
        result="success",
    )


# ── Blocklist CRUD ──────────────────────────────────────────────────────────


@router.get("/blocklists", response_model=list[BlockListResponse])
async def list_blocklists(db: DB, _: CurrentUser) -> list[BlockListResponse]:
    result = await db.execute(
        select(DNSBlockList)
        .options(
            selectinload(DNSBlockList.server_groups),
            selectinload(DNSBlockList.views),
        )
        .order_by(DNSBlockList.name)
    )
    return [_to_response(bl) for bl in result.scalars().all()]


@router.post(
    "/blocklists",
    response_model=BlockListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_blocklist(
    body: BlockListCreate, db: DB, current_user: SuperAdmin
) -> BlockListResponse:
    existing = await db.execute(select(DNSBlockList).where(DNSBlockList.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A blocklist with that name already exists")

    bl = DNSBlockList(**body.model_dump())
    db.add(bl)
    await db.flush()
    db.add(
        _audit(
            current_user,
            "create",
            "dns_blocklist",
            str(bl.id),
            bl.name,
        )
    )
    await db.commit()
    reloaded = await _require_list(bl.id, db)
    logger.info("dns_blocklist_created", list_id=str(bl.id), name=bl.name)
    return _to_response(reloaded)


@router.get("/blocklists/{list_id}", response_model=BlockListResponse)
async def get_blocklist(list_id: uuid.UUID, db: DB, _: CurrentUser) -> BlockListResponse:
    bl = await _require_list(list_id, db)
    return _to_response(bl)


@router.put("/blocklists/{list_id}", response_model=BlockListResponse)
async def update_blocklist(
    list_id: uuid.UUID, body: BlockListUpdate, db: DB, current_user: SuperAdmin
) -> BlockListResponse:
    bl = await _require_list(list_id, db)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(bl, k, v)
    db.add(
        _audit(
            current_user,
            "update",
            "dns_blocklist",
            str(bl.id),
            bl.name,
            changed_fields=list(changes.keys()),
        )
    )
    await db.commit()
    reloaded = await _require_list(list_id, db)
    return _to_response(reloaded)


@router.delete("/blocklists/{list_id}", status_code=204)
async def delete_blocklist(list_id: uuid.UUID, db: DB, current_user: SuperAdmin) -> None:
    bl = await _require_list(list_id, db)
    db.add(_audit(current_user, "delete", "dns_blocklist", str(bl.id), bl.name))
    await db.delete(bl)
    await db.commit()


# ── Assignments ─────────────────────────────────────────────────────────────


@router.put("/blocklists/{list_id}/assignments", response_model=BlockListResponse)
async def update_assignments(
    list_id: uuid.UUID,
    body: AssignmentUpdate,
    db: DB,
    current_user: SuperAdmin,
) -> BlockListResponse:
    bl = await _require_list(list_id, db)

    changed: list[str] = []
    if body.server_group_ids is not None:
        groups = list(
            (
                await db.execute(
                    select(DNSServerGroup).where(DNSServerGroup.id.in_(body.server_group_ids))
                )
            )
            .scalars()
            .all()
        )
        if len(groups) != len(set(body.server_group_ids)):
            raise HTTPException(status_code=404, detail="One or more server groups not found")
        bl.server_groups = groups
        changed.append("server_group_ids")

    if body.view_ids is not None:
        views = list(
            (await db.execute(select(DNSView).where(DNSView.id.in_(body.view_ids)))).scalars().all()
        )
        if len(views) != len(set(body.view_ids)):
            raise HTTPException(status_code=404, detail="One or more views not found")
        bl.views = views
        changed.append("view_ids")

    db.add(
        _audit(
            current_user,
            "update",
            "dns_blocklist_assignment",
            str(bl.id),
            bl.name,
            changed_fields=changed,
        )
    )
    await db.commit()
    reloaded = await _require_list(list_id, db)
    return _to_response(reloaded)


# ── Entries ────────────────────────────────────────────────────────────────


@router.get("/blocklists/{list_id}/entries", response_model=EntryPage)
async def list_entries(
    list_id: uuid.UUID,
    db: DB,
    _: CurrentUser,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> EntryPage:
    await _require_list(list_id, db)
    stmt = select(DNSBlockListEntry).where(DNSBlockListEntry.list_id == list_id)
    if q:
        stmt = stmt.where(DNSBlockListEntry.domain.ilike(f"%{q.lower()}%"))
    total_stmt = stmt
    total = len((await db.execute(total_stmt)).scalars().all())
    items_result = await db.execute(
        stmt.order_by(DNSBlockListEntry.domain).limit(limit).offset(offset)
    )
    return EntryPage(
        total=total,
        items=[EntryResponse.model_validate(e) for e in items_result.scalars().all()],
    )


@router.post(
    "/blocklists/{list_id}/entries",
    response_model=EntryResponse,
    status_code=201,
)
async def add_entry(
    list_id: uuid.UUID, body: EntryCreate, db: DB, current_user: SuperAdmin
) -> EntryResponse:
    bl = await _require_list(list_id, db)
    domain = body.domain.strip().lower().strip(".")
    if not domain or "." not in domain:
        raise HTTPException(status_code=422, detail="Invalid domain")

    existing = await db.execute(
        select(DNSBlockListEntry).where(
            DNSBlockListEntry.list_id == list_id, DNSBlockListEntry.domain == domain
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Domain already in this blocklist")

    entry = DNSBlockListEntry(
        list_id=list_id,
        domain=domain,
        entry_type=body.entry_type,
        target=body.target,
        source="manual",
        is_wildcard=body.is_wildcard,
    )
    db.add(entry)
    bl.entry_count = bl.entry_count + 1
    db.add(
        _audit(
            current_user,
            "create",
            "dns_blocklist_entry",
            str(entry.id),
            f"{domain} ({bl.name})",
        )
    )
    await db.commit()
    await db.refresh(entry)
    return EntryResponse.model_validate(entry)


@router.post(
    "/blocklists/{list_id}/entries/bulk",
    response_model=BulkAddResponse,
)
async def bulk_add_entries(
    list_id: uuid.UUID,
    body: BulkAddRequest,
    db: DB,
    current_user: SuperAdmin,
) -> BulkAddResponse:
    bl = await _require_list(list_id, db)

    # Dedupe submitted domains + validate
    incoming = dedupe_domains(body.domains)

    # Fetch existing domains in the list for the diff
    existing_result = await db.execute(
        select(DNSBlockListEntry.domain).where(DNSBlockListEntry.list_id == list_id)
    )
    existing: set[str] = set(existing_result.scalars().all())

    added = 0
    skipped = 0
    for d in incoming:
        if d in existing:
            skipped += 1
            continue
        db.add(
            DNSBlockListEntry(
                list_id=list_id,
                domain=d,
                entry_type=body.entry_type,
                target=body.target,
                source="manual",
                is_wildcard=body.is_wildcard,
            )
        )
        existing.add(d)
        added += 1

    # Non-canonical / duplicate inputs that got filtered by dedupe:
    skipped += max(0, len(body.domains) - len(incoming))

    bl.entry_count = bl.entry_count + added
    db.add(
        _audit(
            current_user,
            "bulk_create",
            "dns_blocklist_entry",
            str(list_id),
            f"{added} added to {bl.name}",
        )
    )
    await db.commit()
    return BulkAddResponse(added=added, skipped=skipped, total=len(body.domains))


@router.put(
    "/blocklists/{list_id}/entries/{entry_id}",
    response_model=EntryResponse,
)
async def update_entry(
    list_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: EntryUpdate,
    db: DB,
    current_user: SuperAdmin,
) -> EntryResponse:
    bl = await _require_list(list_id, db)
    result = await db.execute(
        select(DNSBlockListEntry).where(
            DNSBlockListEntry.id == entry_id, DNSBlockListEntry.list_id == list_id
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    # Editing is only meaningful for manual entries; feed-sourced entries are
    # overwritten on the next refresh so silent edits would be lost.
    if entry.source != "manual":
        raise HTTPException(
            status_code=409,
            detail="Only manual entries can be edited; feed-sourced entries are refreshed from source.",
        )
    changes = body.model_dump(exclude_none=True)
    if "domain" in changes:
        domain = changes["domain"].strip().lower().strip(".")
        if not domain or "." not in domain:
            raise HTTPException(status_code=422, detail="Invalid domain")
        if domain != entry.domain:
            dup = await db.execute(
                select(DNSBlockListEntry).where(
                    DNSBlockListEntry.list_id == list_id,
                    DNSBlockListEntry.domain == domain,
                    DNSBlockListEntry.id != entry_id,
                )
            )
            if dup.scalar_one_or_none():
                raise HTTPException(
                    status_code=409, detail="Domain already in this blocklist"
                )
        changes["domain"] = domain
    for k, v in changes.items():
        setattr(entry, k, v)
    db.add(
        _audit(
            current_user,
            "update",
            "dns_blocklist_entry",
            str(entry.id),
            f"{entry.domain} ({bl.name})",
        )
    )
    await db.commit()
    await db.refresh(entry)
    return EntryResponse.model_validate(entry)


@router.delete("/blocklists/{list_id}/entries/{entry_id}", status_code=204)
async def delete_entry(
    list_id: uuid.UUID,
    entry_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
) -> None:
    bl = await _require_list(list_id, db)
    result = await db.execute(
        select(DNSBlockListEntry).where(
            DNSBlockListEntry.id == entry_id, DNSBlockListEntry.list_id == list_id
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    db.add(
        _audit(
            current_user,
            "delete",
            "dns_blocklist_entry",
            str(entry.id),
            f"{entry.domain} ({bl.name})",
        )
    )
    await db.delete(entry)
    bl.entry_count = max(0, bl.entry_count - 1)
    await db.commit()


# ── Exceptions ─────────────────────────────────────────────────────────────


@router.get("/blocklists/{list_id}/exceptions", response_model=list[ExceptionResponse])
async def list_exceptions(
    list_id: uuid.UUID, db: DB, _: CurrentUser
) -> list[DNSBlockListException]:
    await _require_list(list_id, db)
    result = await db.execute(
        select(DNSBlockListException)
        .where(DNSBlockListException.list_id == list_id)
        .order_by(DNSBlockListException.domain)
    )
    return list(result.scalars().all())


@router.post(
    "/blocklists/{list_id}/exceptions",
    response_model=ExceptionResponse,
    status_code=201,
)
async def add_exception(
    list_id: uuid.UUID,
    body: ExceptionCreate,
    db: DB,
    current_user: SuperAdmin,
) -> DNSBlockListException:
    bl = await _require_list(list_id, db)
    domain = body.domain.strip().lower().strip(".")
    if not domain or "." not in domain:
        raise HTTPException(status_code=422, detail="Invalid domain")

    existing = await db.execute(
        select(DNSBlockListException).where(
            DNSBlockListException.list_id == list_id,
            DNSBlockListException.domain == domain,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Exception already exists")

    ex = DNSBlockListException(
        list_id=list_id,
        domain=domain,
        reason=body.reason,
        created_by_user_id=current_user.id,
    )
    db.add(ex)
    db.add(
        _audit(
            current_user,
            "create",
            "dns_blocklist_exception",
            str(ex.id),
            f"{domain} ({bl.name})",
        )
    )
    await db.commit()
    await db.refresh(ex)
    return ex


@router.put(
    "/blocklists/{list_id}/exceptions/{exception_id}",
    response_model=ExceptionResponse,
)
async def update_exception(
    list_id: uuid.UUID,
    exception_id: uuid.UUID,
    body: ExceptionUpdate,
    db: DB,
    current_user: SuperAdmin,
) -> DNSBlockListException:
    bl = await _require_list(list_id, db)
    result = await db.execute(
        select(DNSBlockListException).where(
            DNSBlockListException.id == exception_id,
            DNSBlockListException.list_id == list_id,
        )
    )
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exception not found")
    changes = body.model_dump(exclude_none=True)
    if "domain" in changes:
        domain = changes["domain"].strip().lower().strip(".")
        if not domain or "." not in domain:
            raise HTTPException(status_code=422, detail="Invalid domain")
        if domain != ex.domain:
            dup = await db.execute(
                select(DNSBlockListException).where(
                    DNSBlockListException.list_id == list_id,
                    DNSBlockListException.domain == domain,
                    DNSBlockListException.id != exception_id,
                )
            )
            if dup.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="Exception already exists")
        changes["domain"] = domain
    for k, v in changes.items():
        setattr(ex, k, v)
    db.add(
        _audit(
            current_user,
            "update",
            "dns_blocklist_exception",
            str(ex.id),
            f"{ex.domain} ({bl.name})",
        )
    )
    await db.commit()
    await db.refresh(ex)
    return ex


@router.delete("/blocklists/{list_id}/exceptions/{exception_id}", status_code=204)
async def delete_exception(
    list_id: uuid.UUID,
    exception_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
) -> None:
    bl = await _require_list(list_id, db)
    result = await db.execute(
        select(DNSBlockListException).where(
            DNSBlockListException.id == exception_id,
            DNSBlockListException.list_id == list_id,
        )
    )
    ex = result.scalar_one_or_none()
    if not ex:
        raise HTTPException(status_code=404, detail="Exception not found")
    db.add(
        _audit(
            current_user,
            "delete",
            "dns_blocklist_exception",
            str(ex.id),
            f"{ex.domain} ({bl.name})",
        )
    )
    await db.delete(ex)
    await db.commit()


# ── Feed refresh ───────────────────────────────────────────────────────────


@router.post("/blocklists/{list_id}/refresh", response_model=RefreshResponse)
async def refresh_blocklist(
    list_id: uuid.UUID, db: DB, current_user: SuperAdmin
) -> RefreshResponse:
    bl = await _require_list(list_id, db)
    if not bl.feed_url:
        raise HTTPException(
            status_code=422,
            detail="Blocklist has no feed_url — cannot refresh from feed",
        )
    # Lazy import so tests can run without broker connectivity
    from app.tasks.dns import refresh_blocklist_feed

    task_id: str | None = None
    try:
        result = refresh_blocklist_feed.delay(str(bl.id))
        task_id = result.id
    except Exception as e:  # noqa: BLE001
        logger.warning("blocklist_refresh_enqueue_failed", list_id=str(bl.id), error=str(e))

    db.add(
        _audit(
            current_user,
            "refresh",
            "dns_blocklist",
            str(bl.id),
            bl.name,
        )
    )
    await db.commit()
    return RefreshResponse(list_id=bl.id, task_id=task_id, status="queued")


# ── Effective list (backend-neutral; consumed by DNS driver in Wave 2) ─────


@router.get(
    "/blocklists/effective/view/{view_id}",
    response_model=EffectiveBlocklistResponse,
)
async def effective_for_view(
    view_id: uuid.UUID, db: DB, _: CurrentUser
) -> EffectiveBlocklistResponse:
    eff = await build_effective_for_view(db, view_id)
    return EffectiveBlocklistResponse(
        scope=eff.scope,
        scope_id=eff.scope_id,
        entries=[EffectiveEntryResponse(**e.__dict__) for e in eff.entries],
        exceptions=sorted(eff.exceptions),
        lists=eff.lists,
    )


@router.get(
    "/blocklists/effective/group/{group_id}",
    response_model=EffectiveBlocklistResponse,
)
async def effective_for_group(
    group_id: uuid.UUID, db: DB, _: CurrentUser
) -> EffectiveBlocklistResponse:
    eff = await build_effective_for_group(db, group_id)
    return EffectiveBlocklistResponse(
        scope=eff.scope,
        scope_id=eff.scope_id,
        entries=[EffectiveEntryResponse(**e.__dict__) for e in eff.entries],
        exceptions=sorted(eff.exceptions),
        lists=eff.lists,
    )
