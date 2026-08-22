"""System-level admin surface (issue #116).

Exposes factory-reset at ``/system/factory-reset/*`` (#116) and the
support bundle at ``/system/support-bundle*`` (#875). New top-level
system endpoints land under this prefix.
"""

from fastapi import APIRouter

from app.api.v1.system.factory_reset import router as factory_reset_router
from app.api.v1.system.support_bundle import router as support_bundle_router

router = APIRouter()
router.include_router(factory_reset_router, prefix="/factory-reset")
router.include_router(support_bundle_router, prefix="/support-bundle")

__all__ = ["router"]
