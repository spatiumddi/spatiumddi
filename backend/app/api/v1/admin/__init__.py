"""Admin-only endpoints — soft-delete trash, future system-wide tooling."""

from app.api.v1.admin.trash import router as trash_router

__all__ = ["trash_router"]
