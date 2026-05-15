"""Appliance management — Phase 4 root router.

Phase 4a landed visibility gating + ``/info``. Sub-phases attach their
endpoints by including a sub-router into this APIRouter — keeps each
surface (TLS / releases / containers / etc.) in its own file but they
all share the same ``/api/v1/appliance`` prefix + permission gate.

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

from app.api.v1.appliance.containers import router as containers_router
from app.api.v1.appliance.diagnostics import router as diagnostics_router
from app.api.v1.appliance.pairing import router as pairing_router
from app.api.v1.appliance.releases import router as releases_router
from app.api.v1.appliance.slot import router as slot_router
from app.api.v1.appliance.slot_images import router as slot_images_router
from app.api.v1.appliance.supervisor import router as supervisor_router
from app.api.v1.appliance.system import router as system_router
from app.api.v1.appliance.tls import router as tls_router
from app.config import settings
from app.core.permissions import require_permission

router = APIRouter()

# Sub-routers — each Phase 4 sub-surface adds itself here as its
# file lands. Prefixes are scoped under the appliance namespace so
# the URL stays /api/v1/appliance/<surface>/...
router.include_router(tls_router, prefix="/tls")
router.include_router(releases_router, prefix="/releases")
router.include_router(slot_router, prefix="/slot-upgrade")
# Slot-image upload for air-gapped fleets — routes already include
# the ``/slot-images`` prefix in their decorators, so no nested
# prefix here (matches the supervisor + pairing routers' shape).
router.include_router(slot_images_router)
router.include_router(containers_router, prefix="/containers")
router.include_router(diagnostics_router, prefix="/diagnostics")
router.include_router(system_router, prefix="/system")
# Pairing routes use no nested prefix so the URLs are
# ``/api/v1/appliance/pairing-codes`` + ``/api/v1/appliance/pair``
# rather than burying them under a redundant ``/pairing/`` segment.
router.include_router(pairing_router)
# Supervisor register routes — same no-prefix shape as pairing so the
# URL is ``/api/v1/appliance/supervisor/register`` rather than
# ``/api/v1/appliance/supervisor/supervisor/register``.
router.include_router(supervisor_router)


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
