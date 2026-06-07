"""Operator Copilot get_redis_stats tool (#358).

Registration metadata + the superadmin gate + the response shape
(redis INFO summary + wake-bus block). The wake-bus half rides
``agent_wake.get_wake_metrics`` (unit-tested in test_agent_wake.py);
here we just confirm the tool wires both halves and gates correctly.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import app.services.ai.tools.redis as redis_tool
from app.models.auth import User
from app.services.ai.tools.base import REGISTRY


async def _user(db: AsyncSession, *, superadmin: bool) -> User:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password="x",
        is_superadmin=superadmin,
    )
    u.groups = []  # mark loaded — is_effective_superadmin walks .groups (#351)
    db.add(u)
    await db.flush()
    return u


def test_registration_metadata() -> None:
    t = REGISTRY.get("get_redis_stats")
    assert t is not None
    # Operationally-sensitive infra telemetry → opt-in (default-disabled),
    # module-tagged with diagnostics, read-only.
    assert t.default_enabled is False
    assert t.module == "diagnostics"
    assert t.writes is False


async def test_non_superadmin_blocked(db_session: AsyncSession) -> None:
    u = await _user(db_session, superadmin=False)
    res = await redis_tool.get_redis_stats(db_session, u, redis_tool.GetRedisStatsArgs())
    assert "error" in res
    assert "redis" not in res


async def test_superadmin_returns_redis_and_wake_bus(db_session: AsyncSession) -> None:
    u = await _user(db_session, superadmin=True)
    res: dict[str, Any] = await redis_tool.get_redis_stats(
        db_session, u, redis_tool.GetRedisStatsArgs()
    )
    # Both halves present regardless of whether Redis is reachable (each
    # carries its own ``available`` flag — the tool never raises into chat).
    assert "redis" in res
    assert "wake_bus" in res
    assert "available" in res["redis"]
    assert "available" in res["wake_bus"]
