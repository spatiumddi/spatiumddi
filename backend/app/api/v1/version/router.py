"""Running-version + release-check endpoint.

Public (no auth) because the sidebar renders before login and the
release-check banner is visible even on the login screen. The response
is tiny and cacheable; no sensitive data leaks beyond the version
string itself — which is also stamped on every Docker image's OCI
label anyway.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import DB
from app.config import settings
from app.models.settings import PlatformSettings

router = APIRouter(tags=["version"])

_SINGLETON_ID = 1


class VersionResponse(BaseModel):
    # The running version (the image tag / env var the operator set).
    version: str
    # GitHub release-check result. All nullable so a deployment with
    # the release check disabled (or that's never successfully checked)
    # reports a clean "no data yet" state rather than misleading zeros.
    latest_version: str | None
    update_available: bool
    latest_release_url: str | None
    latest_checked_at: datetime | None
    # Set when the release check is turned off — so the UI can
    # distinguish "never checked" from "operator opted out".
    release_check_enabled: bool
    # Most recent error, if any. Null on success or if the check has
    # never run.
    latest_check_error: str | None
    # Appliance-mode signal — true when the API runs on the SpatiumDDI
    # OS appliance ISO (set by the appliance compose env). Gates the
    # "Appliance" sidebar entry + /api/v1/appliance/* router family.
    # Phase 4, issue #134. On plain Docker / K8s deploys this is false
    # and the appliance management surface stays hidden.
    appliance_mode: bool
    appliance_version: str | None
    appliance_hostname: str | None


def _appliance_fields() -> dict[str, str | bool | None]:
    return {
        "appliance_mode": settings.appliance_mode,
        "appliance_version": settings.appliance_version or None,
        "appliance_hostname": settings.appliance_hostname or None,
    }


@router.get("", response_model=VersionResponse)
async def get_version(db: DB) -> VersionResponse:
    ps = await db.get(PlatformSettings, _SINGLETON_ID)
    if ps is None:
        # Settings row hasn't been seeded yet (first boot before
        # /admin/settings is opened). Report the runtime version and
        # leave everything else empty — no fabricated state.
        return VersionResponse(
            version=settings.version,
            latest_version=None,
            update_available=False,
            latest_release_url=None,
            latest_checked_at=None,
            release_check_enabled=True,
            latest_check_error=None,
            **_appliance_fields(),
        )
    return VersionResponse(
        version=settings.version,
        latest_version=ps.latest_version,
        update_available=ps.update_available,
        latest_release_url=ps.latest_release_url,
        latest_checked_at=ps.latest_checked_at,
        release_check_enabled=ps.github_release_check_enabled,
        latest_check_error=ps.latest_check_error,
        **_appliance_fields(),
    )
