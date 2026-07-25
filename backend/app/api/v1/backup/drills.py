"""Restore-verification drill endpoints (issue #702).

Mounted at ``/backup/drills`` by the parent backup router. All
gated to superadmin, matching the rest of the backup surface: a
drill result names the destination and the archive, which is the
same class of metadata the targets endpoints protect.

Drills are read-mostly — the schedule itself lives on the backup
target (``drill_enabled`` / ``drill_cron``, set through
``PATCH /backup/targets/{id}``), so the only mutation here is the
manual "run one now" kick.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from app.api.deps import DB, CurrentUser
from app.core.demo_mode import forbid_in_demo_mode
from app.core.permissions import is_effective_superadmin
from app.models.audit import AuditLog
from app.models.backup import BackupTarget, RestoreDrill
from app.services.backup.drill import run_drill_for_target

router = APIRouter()


def _require_superadmin(current_user: CurrentUser) -> None:
    if not is_effective_superadmin(current_user):
        raise HTTPException(
            status_code=403,
            detail="restore drills are restricted to superadmin users",
        )


class RestoreDrillResponse(BaseModel):
    id: uuid.UUID
    target_id: uuid.UUID
    target_name: str | None = None
    state: str
    triggered_by: str
    filename: str | None
    archive_bytes: int | None
    manifest: dict[str, Any] | None
    scratch_db: str | None
    assertions: list[dict[str, Any]]
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


def _to_response(row: RestoreDrill, *, target_name: str | None = None) -> RestoreDrillResponse:
    return RestoreDrillResponse(
        id=row.id,
        target_id=row.target_id,
        target_name=target_name,
        state=row.state,
        triggered_by=row.triggered_by,
        filename=row.filename,
        archive_bytes=row.archive_bytes,
        manifest=row.manifest,
        scratch_db=row.scratch_db,
        assertions=row.assertions or [],
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
    )


@router.get("", response_model=list[RestoreDrillResponse])
async def list_drills(
    db: DB,
    current_user: CurrentUser,
    target_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[RestoreDrillResponse]:
    """Recent drills, newest first, optionally scoped to one target."""
    _require_superadmin(current_user)
    stmt = (
        select(RestoreDrill, BackupTarget.name)
        .join(BackupTarget, BackupTarget.id == RestoreDrill.target_id)
        .order_by(desc(RestoreDrill.started_at))
        .limit(limit)
    )
    if target_id is not None:
        stmt = stmt.where(RestoreDrill.target_id == target_id)
    if state is not None:
        stmt = stmt.where(RestoreDrill.state == state)
    rows = (await db.execute(stmt)).all()
    return [_to_response(row, target_name=name) for row, name in rows]


@router.get("/{drill_id}", response_model=RestoreDrillResponse)
async def get_drill(drill_id: uuid.UUID, db: DB, current_user: CurrentUser) -> RestoreDrillResponse:
    _require_superadmin(current_user)
    row = await db.get(RestoreDrill, drill_id)
    if row is None:
        raise HTTPException(status_code=404, detail="restore drill not found")
    target = await db.get(BackupTarget, row.target_id)
    return _to_response(row, target_name=target.name if target else None)


@router.post("/run/{target_id}", response_model=RestoreDrillResponse)
async def run_drill_now(
    target_id: uuid.UUID, db: DB, current_user: CurrentUser
) -> RestoreDrillResponse:
    """Run one drill against this target now.

    Synchronous — blocks until the archive is downloaded, replayed
    into a scratch database, asserted against, and the scratch
    database dropped. That can take as long as a real restore on a
    large install; the UI polls the row rather than holding the
    request open where it matters.
    """
    _require_superadmin(current_user)
    forbid_in_demo_mode("restore drills")

    # A drill replays into a throwaway database and never writes to
    # the live one, so it is safe alongside most operations — but a
    # rolling upgrade moves the schema underneath the assertions,
    # which would produce a confusing false verdict.
    from app.services.upgrades.safety import (  # noqa: PLC0415
        assert_no_upgrade_in_flight,
    )

    await assert_no_upgrade_in_flight(db, operation_hint="restore drill")

    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    if row.drill_last_status == "in_progress":
        raise HTTPException(
            status_code=409,
            detail="a drill is already running against this target",
        )

    drill = await run_drill_for_target(db, target=row, triggered_by="manual")
    db.add(
        AuditLog(
            action="backup_restore_drill",
            resource_type="backup_target",
            resource_id=str(row.id),
            resource_display=row.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success" if drill.state == "passed" else "failure",
            error_detail=drill.error,
            new_value={
                "drill_id": str(drill.id),
                "state": drill.state,
                "filename": drill.filename,
                "duration_ms": drill.duration_ms,
                "assertions": drill.assertions,
            },
        )
    )
    await db.commit()
    await db.refresh(drill)
    return _to_response(drill, target_name=row.name)
