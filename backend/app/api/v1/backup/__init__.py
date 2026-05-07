from fastapi import APIRouter

from app.api.v1.backup.router import router as _root
from app.api.v1.backup.targets import router as _targets

# Mount the targets sub-router under ``/backup/targets``. The
# parent router stays at ``/backup`` (download / restore /
# manifest-preview).
router = APIRouter()
router.include_router(_root)
router.include_router(_targets, prefix="/targets")

__all__ = ["router"]
