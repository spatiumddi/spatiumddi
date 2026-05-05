"""Application catalog CRUD — issue #95.

Curated SaaS / voice / video apps used by ``routing_policy`` rows
when ``match_kind=application``. Builtin rows (seeded at startup by
``services.applications.seed_builtin_applications``) are protected:
operators can extend the catalog with custom rows but can't delete or
reword the platform-owned ones — those refresh on every boot.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.api.v1.ownership._audit import write_audit
from app.core.permissions import require_resource_permission
from app.models.overlay import APPLICATION_CATEGORIES, ApplicationCategory

router = APIRouter(
    tags=["applications"],
    dependencies=[Depends(require_resource_permission("application_category"))],
)

ApplicationKind = Literal[
    "saas",
    "voice",
    "video",
    "file_transfer",
    "security",
    "collaboration",
    "ml",
    "custom",
]


class ApplicationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    default_dscp: int | None = Field(default=None, ge=0, le=63)
    category: ApplicationKind = "saas"

    @field_validator("category")
    @classmethod
    def _v_category(cls, v: str) -> str:
        if v not in APPLICATION_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(APPLICATION_CATEGORIES)}")
        return v

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        # Normalise to lowercase + underscores so policy match_value
        # comparisons are stable regardless of how the operator typed
        # the name in the form.
        return v.strip().lower().replace(" ", "_")


class ApplicationUpdate(BaseModel):
    description: str | None = None
    default_dscp: int | None = Field(default=None, ge=0, le=63)
    category: ApplicationKind | None = None


class ApplicationRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    default_dscp: int | None
    category: str
    is_builtin: bool
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


class ApplicationListResponse(BaseModel):
    items: list[ApplicationRead]
    total: int


@router.get("", response_model=ApplicationListResponse)
async def list_applications(
    db: DB,
    _: CurrentUser,
    category: ApplicationKind | None = Query(default=None),
    builtin: bool | None = Query(
        default=None,
        description="Filter to builtin (true) or operator-added (false) rows.",
    ),
    search: str | None = Query(default=None),
) -> ApplicationListResponse:
    stmt = select(ApplicationCategory)
    if category is not None:
        stmt = stmt.where(ApplicationCategory.category == category)
    if builtin is not None:
        stmt = stmt.where(ApplicationCategory.is_builtin.is_(builtin))
    if search:
        needle = f"%{search.strip()}%"
        stmt = stmt.where(ApplicationCategory.name.ilike(needle))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(ApplicationCategory.name.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return ApplicationListResponse(
        items=[ApplicationRead.model_validate(r) for r in rows],
        total=total,
    )


@router.post("", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_application(body: ApplicationCreate, db: DB, user: CurrentUser) -> ApplicationRead:
    existing = await db.scalar(
        select(ApplicationCategory).where(ApplicationCategory.name == body.name)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Application '{body.name}' already exists")
    row = ApplicationCategory(
        name=body.name,
        description=body.description,
        default_dscp=body.default_dscp,
        category=body.category,
        is_builtin=False,
    )
    db.add(row)
    await db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        resource_type="application_category",
        resource_id=str(row.id),
        resource_display=row.name,
        new_value=body.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return ApplicationRead.model_validate(row)


@router.put("/{app_id:uuid}", response_model=ApplicationRead)
async def update_application(
    app_id: uuid.UUID, body: ApplicationUpdate, db: DB, user: CurrentUser
) -> ApplicationRead:
    row = await db.get(ApplicationCategory, app_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if row.is_builtin:
        # Builtin rows are owned by the platform — fields refresh on
        # every boot from ``BUILTIN_APPLICATIONS``. Operators must
        # clone (create a new row) if they want to override.
        raise HTTPException(
            status_code=409,
            detail=(
                "Builtin applications are platform-owned — clone (create a new row) "
                "to customise."
            ),
        )
    changes = body.model_dump(exclude_unset=True)
    if "category" in changes and changes["category"] not in APPLICATION_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"category must be one of {sorted(APPLICATION_CATEGORIES)}",
        )
    for k, v in changes.items():
        setattr(row, k, v)
    write_audit(
        db,
        user=user,
        action="update",
        resource_type="application_category",
        resource_id=str(row.id),
        resource_display=row.name,
        changed_fields=list(changes.keys()),
    )
    await db.commit()
    await db.refresh(row)
    return ApplicationRead.model_validate(row)


@router.delete("/{app_id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application(app_id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    row = await db.get(ApplicationCategory, app_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if row.is_builtin:
        raise HTTPException(
            status_code=409,
            detail="Builtin applications cannot be deleted — they refresh on every boot.",
        )
    write_audit(
        db,
        user=user,
        action="delete",
        resource_type="application_category",
        resource_id=str(row.id),
        resource_display=row.name,
    )
    await db.delete(row)
    await db.commit()


# Suppress ``Any`` unused-import warning when the module is read in
# isolation — kept for future search-filter expansion.
_ = Any


__all__ = ["router"]
