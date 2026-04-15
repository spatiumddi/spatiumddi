"""Audit log read endpoints (superadmin only)."""

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select

from app.api.deps import DB, SuperAdmin
from app.models.audit import AuditLog

router = APIRouter()


class AuditLogResponse(BaseModel):
    id: str
    timestamp: str
    user_display_name: str
    auth_source: str
    action: str
    resource_type: str
    resource_id: str
    resource_display: str
    result: str
    source_ip: str | None = None

    model_config = {"from_attributes": True}

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id(cls, v: object) -> str:
        return str(v)

    @field_validator("timestamp", mode="before")
    @classmethod
    def coerce_ts(cls, v: object) -> str:
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)


class AuditLogPage(BaseModel):
    total: int
    items: list[AuditLogResponse]


@router.get("", response_model=AuditLogPage)
async def list_audit_log(
    current_user: SuperAdmin,
    db: DB,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    user_display_name: str | None = Query(default=None),
) -> AuditLogPage:
    q = select(AuditLog)

    if action:
        q = q.where(AuditLog.action == action)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if user_display_name:
        q = q.where(AuditLog.user_display_name.ilike(f"%{user_display_name}%"))

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    return AuditLogPage(total=total, items=list(rows))
