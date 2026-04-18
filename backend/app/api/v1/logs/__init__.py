"""Logs API — Windows event-log reads over WinRM (future: agent logs,
system logs)."""

from app.api.v1.logs.router import router as logs_router

__all__ = ["logs_router"]
