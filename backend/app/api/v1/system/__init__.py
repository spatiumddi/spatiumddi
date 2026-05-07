"""System-level admin surface (issue #116).

Currently exposes the factory-reset endpoints at
``/system/factory-reset/*``. New top-level system endpoints land
under this prefix.
"""

from fastapi import APIRouter

from app.api.v1.system.factory_reset import router as factory_reset_router

router = APIRouter()
router.include_router(factory_reset_router, prefix="/factory-reset")

__all__ = ["router"]
