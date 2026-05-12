"""Phase 8b-3 — operator-facing slot upgrade endpoint.

Mounted at ``/api/v1/appliance/slot-upgrade``. Endpoints:
    GET    /          — slot status + log tail (polled by UI)
    POST   /apply     — schedule an apply (writes trigger file)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.audit import AuditLog
from app.services.appliance.slot import (
    SlotStatus,
    get_slot_status,
    is_apply_in_flight,
    schedule_apply,
)

import structlog

logger = structlog.get_logger(__name__)

router = APIRouter()


class SlotStatusResponse(BaseModel):
    appliance_mode: bool
    current_slot: str | None
    durable_default: str | None
    is_trial_boot: bool
    upgrade_state: str
    upgrade_state_at: str | None
    log_tail: str


class ApplyRequest(BaseModel):
    image_url: str = Field(
        min_length=1, max_length=2048,
        description="URL or absolute filesystem path to a slot .raw.xz image",
    )
    checksum_url: str | None = Field(
        default=None, max_length=2048,
        description="Optional URL or path to a SHA-256 sidecar (single hash + filename line)",
    )


class ApplyResponse(BaseModel):
    scheduled: str


def _serialise(s: SlotStatus) -> SlotStatusResponse:
    return SlotStatusResponse(
        appliance_mode=s.appliance_mode,
        current_slot=s.current_slot,
        durable_default=s.durable_default,
        is_trial_boot=s.is_trial_boot,
        upgrade_state=s.upgrade_state,
        upgrade_state_at=s.upgrade_state_at,
        log_tail=s.log_tail,
    )


@router.get(
    "",
    response_model=SlotStatusResponse,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Current A/B slot state + upgrade progress",
)
async def get_status() -> SlotStatusResponse:
    return _serialise(get_slot_status())


@router.post(
    "/apply",
    response_model=ApplyResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Schedule a slot upgrade — writes the image to the inactive slot + arms next-boot",
)
async def apply(
    body: ApplyRequest,
    db: DB,
    user: CurrentUser,
) -> ApplyResponse:
    if is_apply_in_flight():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a slot upgrade is already in flight — wait for it to finish",
        )
    try:
        schedule_apply(body.image_url, body.checksum_url)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="apply_slot_upgrade",
            resource_type="appliance",
            resource_id="slot",
            resource_display=body.image_url,
            new_value={"image_url": body.image_url, "checksum_url": body.checksum_url},
            result="success",
        )
    )
    await db.commit()
    logger.info("appliance_slot_upgrade_apply_requested",
                image_url=body.image_url, user=user.username)
    return ApplyResponse(scheduled=body.image_url)
