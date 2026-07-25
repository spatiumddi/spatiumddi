from fastapi import APIRouter

from app.api.v1.backup.drills import router as _drills
from app.api.v1.backup.router import router as _root
from app.api.v1.backup.targets import router as _targets

# Mount the targets sub-router under ``/backup/targets`` and the
# restore-drill sub-router under ``/backup/drills``. The parent
# router stays at ``/backup`` (download / restore /
# manifest-preview).
router = APIRouter()
router.include_router(_root)
router.include_router(_targets, prefix="/targets")
router.include_router(_drills, prefix="/drills")

__all__ = ["router"]
