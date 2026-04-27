"""Trash — list, restore, and (via the existing handlers) permanent-delete
soft-deleted IPAM / DNS / DHCP rows.

The soft-delete stamping happens in the per-resource delete handlers; this
router gives operators a unified view of "what's recoverable", a one-click
restore (atomic on the whole batch), and the housekeeping endpoints the
admin UI needs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DB, SuperAdmin
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.dhcp import DHCPScope
from app.models.dns import DNSRecord, DNSZone
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.soft_delete import (
    SOFT_DELETE_RESOURCE_TYPES,
    TYPE_TO_MODEL,
    default_conflict_check,
    restore_batch,
)

logger = structlog.get_logger(__name__)


router = APIRouter()


# ── Response shapes ───────────────────────────────────────────────────────


class TrashEntry(BaseModel):
    id: uuid.UUID
    type: str
    name_or_cidr: str
    deleted_at: datetime
    deleted_by_user_id: uuid.UUID | None
    deleted_by_username: str | None
    deletion_batch_id: uuid.UUID | None
    batch_size: int


class TrashListResponse(BaseModel):
    items: list[TrashEntry]
    total: int


class RestoreResponse(BaseModel):
    batch_id: uuid.UUID
    restored: int


class RestoreConflict(BaseModel):
    type: str
    id: str
    display: str
    reason: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _row_label(obj: Any) -> str:
    """Mirror of services.soft_delete._row_display, kept here so the trash
    list can label rows without importing that private helper."""
    if isinstance(obj, IPSpace):
        return obj.name
    if isinstance(obj, (IPBlock, Subnet)):
        return f"{obj.network}{(' ' + obj.name) if getattr(obj, 'name', '') else ''}".strip()
    if isinstance(obj, DNSZone):
        return obj.name
    if isinstance(obj, DNSRecord):
        return f"{obj.fqdn} {obj.record_type}"
    if isinstance(obj, DHCPScope):
        return obj.name or str(obj.id)
    return str(getattr(obj, "id", obj))


async def _resolve_usernames(db: Any, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    res = await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
    return {row.id: row.username for row in res.all()}


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/trash", response_model=TrashListResponse)
async def list_trash(
    db: DB,
    current_user: SuperAdmin,
    type: str | None = Query(None, description="Filter to one resource type"),
    since: datetime | None = Query(None, description="Only show rows deleted after this UTC time"),
    until: datetime | None = Query(None, description="Only show rows deleted before this UTC time"),
    q: str | None = Query(None, description="Substring match on name / CIDR / FQDN"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> TrashListResponse:
    """Paginated list of soft-deleted rows across every in-scope type."""

    if type is not None and type not in SOFT_DELETE_RESOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown type {type!r}. Valid: {sorted(SOFT_DELETE_RESOURCE_TYPES)}",
        )

    types_to_query = [type] if type else list(SOFT_DELETE_RESOURCE_TYPES)
    items: list[TrashEntry] = []
    user_ids: set[uuid.UUID] = set()
    batch_size_cache: dict[uuid.UUID, int] = {}

    # Collect rows from every requested model. Each query opts into
    # include_deleted so it sees soft-deleted rows; without that, the
    # global filter hides them.
    for resource_type in types_to_query:
        model = TYPE_TO_MODEL[resource_type]
        stmt: Any = (
            select(model)
            .where(model.deleted_at.is_not(None))
            .execution_options(include_deleted=True)
        )
        if since is not None:
            stmt = stmt.where(model.deleted_at >= since)
        if until is not None:
            stmt = stmt.where(model.deleted_at <= until)
        res = await db.execute(stmt)
        for row in res.scalars().all():
            label = _row_label(row)
            if q and q.lower() not in label.lower():
                continue
            if row.deleted_by_user_id is not None:
                user_ids.add(row.deleted_by_user_id)
            items.append(
                TrashEntry(
                    id=row.id,
                    type=resource_type,
                    name_or_cidr=label,
                    deleted_at=row.deleted_at,
                    deleted_by_user_id=row.deleted_by_user_id,
                    deleted_by_username=None,
                    deletion_batch_id=row.deletion_batch_id,
                    batch_size=0,  # filled after we collect everything
                )
            )

    # Resolve usernames once for all collected rows.
    username_map = await _resolve_usernames(db, user_ids)

    # Compute batch sizes by counting rows per batch_id across every model.
    seen_batches: set[uuid.UUID] = {
        item.deletion_batch_id for item in items if item.deletion_batch_id is not None
    }
    for batch_id in seen_batches:
        size = 0
        for resource_type in SOFT_DELETE_RESOURCE_TYPES:
            model = TYPE_TO_MODEL[resource_type]
            res = await db.execute(
                select(func.count())
                .select_from(model)
                .where(model.deletion_batch_id == batch_id)
                .execution_options(include_deleted=True)
            )
            size += int(res.scalar_one() or 0)
        batch_size_cache[batch_id] = size

    for item in items:
        if item.deletion_batch_id is not None:
            item.batch_size = batch_size_cache.get(item.deletion_batch_id, 1)
        else:
            item.batch_size = 1
        if item.deleted_by_user_id is not None:
            item.deleted_by_username = username_map.get(item.deleted_by_user_id)

    items.sort(key=lambda i: i.deleted_at, reverse=True)
    total = len(items)
    return TrashListResponse(items=items[offset : offset + limit], total=total)


@router.post("/trash/{type}/{row_id}/restore", response_model=RestoreResponse)
async def restore_row(
    type: str,
    row_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
) -> RestoreResponse:
    """Restore a soft-deleted row and every sibling in its deletion batch."""

    if type not in SOFT_DELETE_RESOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown type {type!r}. Valid: {sorted(SOFT_DELETE_RESOURCE_TYPES)}",
        )

    model = TYPE_TO_MODEL[type]
    stmt: Any = (
        select(model)
        .where(model.id == row_id, model.deleted_at.is_not(None))
        .execution_options(include_deleted=True)
    )
    target = (await db.execute(stmt)).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Soft-deleted row not found")

    batch_id = target.deletion_batch_id
    if batch_id is None:
        # Defensive: stamp a fresh batch and restore just this row.
        batch_id = uuid.uuid4()
        target.deletion_batch_id = batch_id

    async def _check(obj: Any) -> str | None:
        return await default_conflict_check(db, obj)

    restored, conflicts = await restore_batch(db, batch_id, conflict_check=_check)
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={"message": "Restore would clash with active rows", "conflicts": conflicts},
        )

    for obj in restored:
        from app.services.soft_delete import _resource_type as resource_type_for  # noqa: PLC0415

        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="restore",
                resource_type=resource_type_for(obj),
                resource_id=str(obj.id),
                resource_display=_row_label(obj),
                new_value={"deletion_batch_id": str(batch_id)},
                result="success",
            )
        )

    await db.commit()
    logger.info(
        "trash.restore",
        batch_id=str(batch_id),
        restored=len(restored),
        user_id=str(current_user.id),
    )
    return RestoreResponse(batch_id=batch_id, restored=len(restored))


@router.delete("/trash/{type}/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def permanent_delete_from_trash(
    type: str,
    row_id: uuid.UUID,
    db: DB,
    current_user: SuperAdmin,
) -> None:
    """Hard-delete a soft-deleted row from the trash.

    Unlike the per-resource ``?permanent=true`` flag (which both deletes
    *and* runs the legacy write-through hooks), this endpoint just removes
    the DB row that's already been soft-deleted. The downstream cleanup
    (Windows DHCP scope removal, DNS zone delete on agentless servers)
    has either already happened or doesn't need to since the row was
    already hidden from queries.
    """

    if type not in SOFT_DELETE_RESOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown type {type!r}. Valid: {sorted(SOFT_DELETE_RESOURCE_TYPES)}",
        )

    model = TYPE_TO_MODEL[type]
    stmt: Any = (
        select(model)
        .where(model.id == row_id, model.deleted_at.is_not(None))
        .execution_options(include_deleted=True)
    )
    target = (await db.execute(stmt)).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Soft-deleted row not found")

    label = _row_label(target)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="permanent_delete",
            resource_type=type,
            resource_id=str(target.id),
            resource_display=label,
            result="success",
        )
    )
    await db.delete(target)
    await db.commit()
