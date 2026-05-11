"""SpatiumDDI release management — Phase 4c.

Mounted at ``/api/v1/appliance/releases``. Three endpoints:
    GET    /                 — list available releases + installed + log tail
    POST   /apply            — schedule an upgrade to the given tag (202)
    GET    /log              — tail the host-side update log (polled by UI)

The actual upgrade runs as a host-side oneshot driven by a systemd
Path unit watching ``/var/lib/spatiumddi/release-pending``; see
``app/services/appliance/releases.py`` for the rationale.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.audit import AuditLog
from app.services.appliance.releases import (
    get_installed_version,
    get_update_log_tail,
    is_apply_in_flight,
    list_releases as svc_list_releases,
    schedule_apply,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


class ReleaseInfo(BaseModel):
    tag: str
    name: str
    published_at: datetime
    body: str
    html_url: str
    is_prerelease: bool
    is_installed: bool


class ReleasesResponse(BaseModel):
    installed_version: str
    apply_in_flight: bool
    releases: list[ReleaseInfo]
    update_log_tail: str


class ApplyRequest(BaseModel):
    tag: str = Field(min_length=1, max_length=80)


class ApplyResponse(BaseModel):
    scheduled: str


@router.get(
    "",
    response_model=ReleasesResponse,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="List available releases + currently-installed version",
)
async def list_releases() -> ReleasesResponse:
    rels = await svc_list_releases()
    return ReleasesResponse(
        installed_version=get_installed_version(),
        apply_in_flight=is_apply_in_flight(),
        releases=[
            ReleaseInfo(
                tag=r.tag,
                name=r.name,
                published_at=r.published_at,
                body=r.body,
                html_url=r.html_url,
                is_prerelease=r.is_prerelease,
                is_installed=r.is_installed,
            )
            for r in rels
        ],
        update_log_tail=get_update_log_tail(),
    )


@router.post(
    "/apply",
    response_model=ApplyResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Schedule an upgrade to the named tag",
)
async def apply_release(
    body: ApplyRequest,
    db: DB,
    user: CurrentUser,
) -> ApplyResponse:
    if is_apply_in_flight():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "an upgrade is already in flight — wait for it to finish",
        )
    try:
        schedule_apply(body.tag)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            action="apply_release",
            resource_type="appliance",
            resource_id="release",
            resource_display=body.tag,
            new_value={"tag": body.tag},
            result="success",
        )
    )
    await db.commit()
    logger.info("appliance_release_apply_requested", tag=body.tag, user=user.username)
    return ApplyResponse(scheduled=body.tag)


@router.get(
    "/log",
    response_model=dict,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Tail of the host-side update log",
)
async def get_log() -> dict:
    return {
        "apply_in_flight": is_apply_in_flight(),
        "log_tail": get_update_log_tail(),
    }
