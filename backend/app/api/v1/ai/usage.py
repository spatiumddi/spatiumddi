"""Operator Copilot usage stats endpoints (issue #90 Wave 4).

* ``GET /api/v1/ai/usage/me`` — the calling user's today-so-far
  totals + the configured per-user caps. Used by the chat drawer
  progress indicator.
* ``GET /api/v1/ai/usage`` — admin aggregate view (today / 7d / 30d
  totals + top users today). SuperAdmin only. Used by the
  platform-insights AI usage card.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.services.ai.usage import (
    UsageSnapshot,
    admin_usage_stats,
    current_user_usage_today,
)

router = APIRouter()


class UsageSnapshotResponse(BaseModel):
    messages: int
    tokens_in: int
    tokens_out: int
    cost_usd: str  # serialized as string to preserve Decimal precision
    cap_token: int | None = None
    cap_cost_usd: str | None = None


class TopUserEntry(BaseModel):
    user_id: str
    username: str
    messages: int
    tokens_in: int
    tokens_out: int
    cost_usd: str


class AdminUsageResponse(BaseModel):
    today: UsageSnapshotResponse
    last_7d: UsageSnapshotResponse
    last_30d: UsageSnapshotResponse
    top_users_today: list[TopUserEntry]


def _snap_to_response(s: UsageSnapshot) -> UsageSnapshotResponse:
    return UsageSnapshotResponse(
        messages=s.messages,
        tokens_in=s.tokens_in,
        tokens_out=s.tokens_out,
        cost_usd=str(s.cost_usd if s.cost_usd is not None else Decimal("0")),
        cap_token=s.cap_token,
        cap_cost_usd=str(s.cap_cost_usd) if s.cap_cost_usd is not None else None,
    )


@router.get("/usage/me", response_model=UsageSnapshotResponse)
async def my_usage_today(current_user: CurrentUser, db: DB) -> UsageSnapshotResponse:
    snap = await current_user_usage_today(db, current_user)
    return _snap_to_response(snap)


@router.get("/usage", response_model=AdminUsageResponse)
async def admin_usage(current_user: SuperAdmin, db: DB) -> AdminUsageResponse:
    stats = await admin_usage_stats(db)
    return AdminUsageResponse(
        today=_snap_to_response(stats.today),
        last_7d=_snap_to_response(stats.last_7d),
        last_30d=_snap_to_response(stats.last_30d),
        top_users_today=[
            TopUserEntry(
                user_id=u["user_id"],
                username=u["username"],
                messages=u["messages"],
                tokens_in=u["tokens_in"],
                tokens_out=u["tokens_out"],
                cost_usd=u["cost_usd"],
            )
            for u in stats.top_users_today
        ],
    )


# Stash unused imports so ruff doesn't flag them; keeps the symbol
# in scope for future Phase-2 work that might add more endpoints
# referencing the underlying ``Any`` type for arbitrary stat shapes.
_ = Any
