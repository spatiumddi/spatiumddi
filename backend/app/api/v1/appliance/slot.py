"""Phase 8b-3 — operator-facing slot upgrade endpoint.

Mounted at ``/api/v1/appliance/slot-upgrade``. Endpoints:
    GET    /            — slot status + log tail (polled by UI)
    POST   /apply       — schedule an apply (writes apply trigger)
    POST   /rollback    — Phase 8c-3: durably switch to the inactive
                          slot (writes rollback trigger; operator
                          reboots when ready)
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.audit import AuditLog
from app.services.appliance.slot import (
    SlotStatus,
    can_rollback,
    get_slot_status,
    is_apply_in_flight,
    schedule_apply,
    schedule_rollback,
)

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
        min_length=1,
        max_length=2048,
        description="URL or absolute filesystem path to a slot .raw.xz image",
    )
    checksum_url: str | None = Field(
        default=None,
        max_length=2048,
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
    logger.info(
        "appliance_slot_upgrade_apply_requested", image_url=body.image_url, user=user.username
    )
    return ApplyResponse(scheduled=body.image_url)


class RollbackRequest(BaseModel):
    target_slot: Literal["slot_a", "slot_b"] | None = Field(
        default=None,
        description=(
            "Explicit slot to commit as the durable default. When omitted, "
            "the host-side runner picks the inactive slot — the typical "
            "'go back to the previously-running slot' intent."
        ),
    )


class RollbackResponse(BaseModel):
    scheduled: str
    target_slot: str | None


@router.post(
    "/rollback",
    response_model=RollbackResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary=(
        "Schedule a slot rollback — durably flips the inactive slot to be the "
        "next-boot default (operator reboots when ready)"
    ),
)
async def rollback(
    body: RollbackRequest,
    db: DB,
    user: CurrentUser,
) -> RollbackResponse:
    """Phase 8c-3 — operator-driven rollback.

    Distinct from the trial-boot auto-revert (which is the kernel-panic
    / health-check safety net during the trial window). This is the
    'I just upgraded and want to revert' button — explicit operator
    choice, no health gate. Calls ``grub-set-default`` durably; the
    swap doesn't take effect until the operator reboots.
    """
    if is_apply_in_flight():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a slot upgrade is in flight — wait for it to finish before rolling back",
        )
    if not can_rollback():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "rollback isn't available — appliance_mode off, or active slot couldn't be detected",
        )

    try:
        schedule_rollback(body.target_slot)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="rollback_slot_upgrade",
            resource_type="appliance",
            resource_id="slot",
            resource_display=body.target_slot or "(inactive)",
            new_value={"target_slot": body.target_slot},
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "appliance_slot_upgrade_rollback_requested",
        target_slot=body.target_slot,
        user=user.username,
    )
    return RollbackResponse(scheduled="rollback", target_slot=body.target_slot)
