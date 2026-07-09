"""Scheduled Wake-on-LAN REST API package — Phase 1 (issue #586).

Exposes ``router`` (mounted at ``/api/v1/wake-scheduler`` behind the
``tools.wake_scheduler`` feature module). See :mod:`.router`.
"""

from __future__ import annotations

from app.api.v1.wol_schedules.router import router

__all__ = ["router"]
