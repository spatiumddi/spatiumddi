"""Diagnostics — uncaught exception viewer (issue #123).

All endpoints are gated to ``is_superadmin``. Tracebacks may carry
internal paths + sanitised-but-still-sensitive context; we don't want
delegated department admins picking that up. A dedicated
``diagnostics:read`` permission can land later if operators need
auditor-style access — for now superadmin is the right blast radius.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from app.api.deps import DB, CurrentUser
from app.core.permissions import is_effective_superadmin
from app.models.auth import User
from app.models.diagnostics import InternalError

router = APIRouter()


def _require_superadmin(user_obj: User) -> None:
    if not is_effective_superadmin(user_obj):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Diagnostics surface is restricted to superadmin",
        )


# ── Schemas ────────────────────────────────────────────────────────────


class InternalErrorResponse(BaseModel):
    id: uuid.UUID
    timestamp: datetime
    service: str
    kind: str
    request_id: str | None
    route_or_task: str | None
    exception_class: str
    message: str
    traceback: str
    context_json: dict[str, Any]
    fingerprint: str
    occurrence_count: int
    last_seen_at: datetime
    acknowledged_by: uuid.UUID | None
    acknowledged_at: datetime | None
    suppressed_until: datetime | None

    class Config:
        from_attributes = True


class InternalErrorListItem(BaseModel):
    """Lighter shape for the list view — leaves traceback +
    context_json out so the table query stays fast on installs with
    thousands of rows. The detail endpoint carries the full payload.
    """

    id: uuid.UUID
    timestamp: datetime
    service: str
    kind: str
    route_or_task: str | None
    exception_class: str
    message: str
    fingerprint: str
    occurrence_count: int
    last_seen_at: datetime
    acknowledged_by: uuid.UUID | None
    acknowledged_at: datetime | None
    suppressed_until: datetime | None

    class Config:
        from_attributes = True


class InternalErrorStats(BaseModel):
    total: int
    unacknowledged: int
    # Unacked errors with ``occurrence_count >= 5`` in the last 24 h.
    # Drives the floating banner on the admin pages.
    noisy_unacked: int


# ── Endpoints ──────────────────────────────────────────────────────────


@router.get("/errors", response_model=list[InternalErrorListItem])
async def list_errors(
    db: DB,
    current_user: CurrentUser,
    service: str | None = Query(None, description="api / worker / beat"),
    acknowledged: str | None = Query(
        None,
        description="yes / no / all (default: all)",
    ),
    since_hours: int | None = Query(
        None,
        ge=1,
        le=24 * 30,
        description="filter to errors with last_seen_at within the last N hours",
    ),
    exception_class: str | None = Query(
        None,
        description="exact match on the dotted class name",
    ),
    limit: int = Query(200, ge=1, le=500),
) -> list[InternalError]:
    _require_superadmin(current_user)
    stmt = select(InternalError).order_by(desc(InternalError.last_seen_at))
    if service:
        stmt = stmt.where(InternalError.service == service)
    if acknowledged == "yes":
        stmt = stmt.where(InternalError.acknowledged_by.isnot(None))
    elif acknowledged == "no":
        stmt = stmt.where(InternalError.acknowledged_by.is_(None))
    if since_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
        stmt = stmt.where(InternalError.last_seen_at >= cutoff)
    if exception_class:
        stmt = stmt.where(InternalError.exception_class == exception_class)
    stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


@router.get("/errors/stats", response_model=InternalErrorStats)
async def error_stats(
    db: DB,
    current_user: CurrentUser,
) -> InternalErrorStats:
    _require_superadmin(current_user)
    total = (await db.execute(select(func.count()).select_from(InternalError))).scalar_one()
    unack = (
        await db.execute(
            select(func.count())
            .select_from(InternalError)
            .where(InternalError.acknowledged_by.is_(None))
        )
    ).scalar_one()
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    noisy = (
        await db.execute(
            select(func.count())
            .select_from(InternalError)
            .where(
                InternalError.acknowledged_by.is_(None),
                InternalError.occurrence_count >= 5,
                InternalError.last_seen_at >= cutoff,
            )
        )
    ).scalar_one()
    return InternalErrorStats(total=total, unacknowledged=unack, noisy_unacked=noisy)


@router.get("/errors/{error_id}", response_model=InternalErrorResponse)
async def get_error(
    error_id: uuid.UUID,
    db: DB,
    current_user: CurrentUser,
) -> InternalError:
    _require_superadmin(current_user)
    row = await db.get(InternalError, error_id)
    if row is None:
        raise HTTPException(status_code=404, detail="error not found")
    return row


class AcknowledgeRequest(BaseModel):
    note: str | None = Field(None, max_length=500)


@router.post("/errors/{error_id}/acknowledge", response_model=InternalErrorResponse)
async def acknowledge_error(
    error_id: uuid.UUID,
    db: DB,
    current_user: CurrentUser,
) -> InternalError:
    _require_superadmin(current_user)
    row = await db.get(InternalError, error_id)
    if row is None:
        raise HTTPException(status_code=404, detail="error not found")
    if row.acknowledged_by is None:
        row.acknowledged_by = current_user.id
        row.acknowledged_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
    return row


class SuppressRequest(BaseModel):
    hours: int = Field(24, ge=1, le=24 * 30)


@router.post("/errors/{error_id}/suppress", response_model=InternalErrorResponse)
async def suppress_error(
    error_id: uuid.UUID,
    body: SuppressRequest,
    db: DB,
    current_user: CurrentUser,
) -> InternalError:
    """Silence the matching fingerprint for ``hours``. While the
    suppression window is active the capture loop still bumps
    ``occurrence_count`` on the existing row, but no new rows are
    inserted. The "Suppress 24h" button in the admin viewer hits
    this with ``hours=24``.
    """
    _require_superadmin(current_user)
    row = await db.get(InternalError, error_id)
    if row is None:
        raise HTTPException(status_code=404, detail="error not found")
    row.suppressed_until = datetime.now(UTC) + timedelta(hours=body.hours)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/errors/{error_id}", status_code=204)
async def delete_error(
    error_id: uuid.UUID,
    db: DB,
    current_user: CurrentUser,
) -> None:
    _require_superadmin(current_user)
    row = await db.get(InternalError, error_id)
    if row is None:
        raise HTTPException(status_code=404, detail="error not found")
    await db.delete(row)
    await db.commit()
