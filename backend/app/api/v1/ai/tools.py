"""Operator-facing tools introspection (issue #90 Wave 2).

Surfaces the registered tool catalog at ``/api/v1/ai/tools`` for
two consumers:

1. The frontend's "what can the copilot do?" panel (Wave 3) — lists
   tools grouped by category, with a description column.
2. Operators sanity-checking that a tool registered correctly after
   adding a new one (the registry runs on ``app`` import).

Read-only and SuperAdmin-gated. Future Phase 2 may relax this if
operators need to introspect what their session can do.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import SuperAdmin
from app.services.ai.tools import REGISTRY

router = APIRouter()


class ToolEntry(BaseModel):
    name: str
    description: str
    category: str
    writes: bool
    parameters_schema: dict[str, Any]


class ToolCatalogResponse(BaseModel):
    tools: list[ToolEntry]
    total: int


@router.get("/tools", response_model=ToolCatalogResponse)
async def list_tools(current_user: SuperAdmin) -> ToolCatalogResponse:
    out = [
        ToolEntry(
            name=t.name,
            description=t.description,
            category=t.category,
            writes=t.writes,
            parameters_schema=t.parameters_schema(),
        )
        for t in REGISTRY.all()
    ]
    return ToolCatalogResponse(tools=out, total=len(out))
