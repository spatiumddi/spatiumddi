"""Appliance management — Phase 4a frame.

Phase 4a (issue #134) lands just the visibility gating + a minimal
``/info`` endpoint so the frontend can render the Appliance management
hub. Every sub-phase below extends the same router family with the
real surfaces (TLS upload, release manager, containers, etc.).

Gating model:

* ``settings.appliance_mode`` — set by the appliance compose env. On
  non-appliance deploys the router stays mounted but ``/info`` reports
  ``appliance_mode=false`` and the frontend hides the sidebar entry.
* ``require_permission("read", "appliance")`` — RBAC gate. The
  "Appliance Operator" builtin role grants ``admin``; superadmin
  always bypasses. Layered on top of ``appliance_mode`` so a deploy
  on plain docker-compose can't smuggle out appliance-only data via
  a stolen non-superadmin token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import settings
from app.core.permissions import require_permission

router = APIRouter()


class ApplianceInfo(BaseModel):
    appliance_mode: bool
    appliance_version: str | None
    appliance_hostname: str | None


@router.get(
    "/info",
    response_model=ApplianceInfo,
    dependencies=[Depends(require_permission("read", "appliance"))],
    summary="Appliance identity + capabilities",
)
async def get_appliance_info() -> ApplianceInfo:
    """Return the appliance's own self-description.

    Distinct from ``/api/v1/version`` (which is public so the sidebar
    can render before login). This endpoint sits behind the appliance
    read permission because once 4b-4g land it grows into a richer
    payload (capability flags, supervised container set, host network
    digest, …) that the unauthenticated surface shouldn't expose.
    """
    return ApplianceInfo(
        appliance_mode=settings.appliance_mode,
        appliance_version=settings.appliance_version or None,
        appliance_hostname=settings.appliance_hostname or None,
    )
