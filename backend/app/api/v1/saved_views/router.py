"""Saved searches / saved views CRUD (issue #77).

Per-user, per-page named filter/sort/column presets. Every row is owned
by the calling user, so the surface scopes by ``user_id`` rather than
the RBAC permission grammar — any authenticated user manages their own
views and can never see another user's. Mutations are audited
(non-negotiable #4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api.deps import DB, CurrentUser
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.saved_view import SavedView

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────


class SavedViewRead(BaseModel):
    id: uuid.UUID
    page: str
    name: str
    payload: dict[str, Any]
    is_default: bool
    created_at: datetime
    modified_at: datetime


class SavedViewCreate(BaseModel):
    page: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class SavedViewUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    payload: dict[str, Any] | None = None
    is_default: bool | None = None


def _to_read(row: SavedView) -> SavedViewRead:
    return SavedViewRead(
        id=row.id,
        page=row.page,
        name=row.name,
        payload=row.payload or {},
        is_default=row.is_default,
        created_at=row.created_at,
        modified_at=row.modified_at,
    )


def _audit(
    db: DB,
    *,
    user: User,
    action: str,
    row: SavedView,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action=action,
            resource_type="saved_view",
            resource_id=str(row.id),
            resource_display=f"{row.page}:{row.name}",
        )
    )


async def _clear_other_defaults(
    db: DB, *, user_id: uuid.UUID, page: str, keep_id: uuid.UUID | None
) -> None:
    """At most one default view per (user, page)."""
    stmt = (
        update(SavedView)
        .where(
            SavedView.user_id == user_id,
            SavedView.page == page,
            SavedView.is_default.is_(True),
        )
        .values(is_default=False)
    )
    if keep_id is not None:
        stmt = stmt.where(SavedView.id != keep_id)
    await db.execute(stmt)


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("", response_model=list[SavedViewRead])
async def list_saved_views(
    current_user: CurrentUser,
    db: DB,
    page: str | None = None,
) -> list[SavedViewRead]:
    """The calling user's saved views, newest first. Optional ``page`` filter."""
    stmt = select(SavedView).where(SavedView.user_id == current_user.id)
    if page is not None:
        stmt = stmt.where(SavedView.page == page)
    stmt = stmt.order_by(SavedView.modified_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_read(r) for r in rows]


@router.post("", response_model=SavedViewRead, status_code=status.HTTP_201_CREATED)
async def create_saved_view(
    body: SavedViewCreate,
    current_user: CurrentUser,
    db: DB,
) -> SavedViewRead:
    # Friendly 409 rather than leaking the DB unique-constraint error.
    existing = (
        await db.execute(
            select(SavedView).where(
                SavedView.user_id == current_user.id,
                SavedView.page == body.page,
                SavedView.name == body.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'A view named "{body.name}" already exists on this page.',
        )

    row = SavedView(
        user_id=current_user.id,
        page=body.page,
        name=body.name,
        payload=body.payload,
        is_default=body.is_default,
    )
    db.add(row)
    await db.flush()
    if body.is_default:
        await _clear_other_defaults(db, user_id=current_user.id, page=body.page, keep_id=row.id)
    _audit(db, user=current_user, action="create", row=row)
    await db.commit()
    await db.refresh(row)
    return _to_read(row)


async def _get_owned(db: DB, view_id: uuid.UUID, user: User) -> SavedView:
    row = await db.get(SavedView, view_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved view not found.")
    return row


@router.patch("/{view_id}", response_model=SavedViewRead)
async def update_saved_view(
    view_id: uuid.UUID,
    body: SavedViewUpdate,
    current_user: CurrentUser,
    db: DB,
) -> SavedViewRead:
    row = await _get_owned(db, view_id, current_user)
    if body.name is not None and body.name != row.name:
        clash = (
            await db.execute(
                select(SavedView).where(
                    SavedView.user_id == current_user.id,
                    SavedView.page == row.page,
                    SavedView.name == body.name,
                    SavedView.id != row.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'A view named "{body.name}" already exists on this page.',
            )
        row.name = body.name
    if body.payload is not None:
        row.payload = body.payload
    if body.is_default is not None:
        row.is_default = body.is_default
        if body.is_default:
            await _clear_other_defaults(db, user_id=current_user.id, page=row.page, keep_id=row.id)
    _audit(db, user=current_user, action="update", row=row)
    await db.commit()
    await db.refresh(row)
    return _to_read(row)


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_view(
    view_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> None:
    row = await _get_owned(db, view_id, current_user)
    _audit(db, user=current_user, action="delete", row=row)
    await db.delete(row)
    await db.commit()
