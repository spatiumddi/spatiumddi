"""Admin-only endpoints — soft-delete trash, postgres insights, container stats."""

from app.api.v1.admin.containers import router as containers_router
from app.api.v1.admin.postgres import router as postgres_router
from app.api.v1.admin.trash import router as trash_router

__all__ = ["containers_router", "postgres_router", "trash_router"]
