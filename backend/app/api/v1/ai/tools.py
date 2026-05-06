"""Operator Copilot tool catalog (issue #90 Wave 2 + #101 follow-up).

Two surfaces:

* ``GET /ai/tools`` — full registry introspection. Returns every
  registered tool with its declared default + the *currently
  effective* enabled flag (after applying
  ``PlatformSettings.ai_tools_enabled``). Drives the Settings →
  AI → Tool Catalog page.
* ``PUT /ai/tools/catalog`` — operator override. Replace the
  platform-level allowlist with an explicit list, or send ``null``
  to revert to per-tool registry defaults. Per-provider
  ``AIProvider.enabled_tools`` narrows further; both layers
  compose.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import DB, SuperAdmin
from app.models.settings import PlatformSettings
from app.services.ai.tools import REGISTRY, effective_tool_names

router = APIRouter()


class ToolEntry(BaseModel):
    name: str
    description: str
    category: str
    writes: bool
    parameters_schema: dict[str, Any]
    # Registry-declared default. Niche tools (TLS check, public
    # WHOIS lookups) ship as False.
    default_enabled: bool
    # Live state — true iff this tool is currently exposed to the
    # LLM after ``PlatformSettings.ai_tools_enabled`` has been
    # applied. Per-provider narrowing is NOT included here (it's
    # surfaced in the per-provider modal).
    enabled: bool


class ToolCatalogResponse(BaseModel):
    """Full tool catalog. ``platform_override`` reflects the raw
    setting: NULL = per-tool defaults, list = explicit allowlist."""

    tools: list[ToolEntry]
    total: int
    platform_override: list[str] | None


class ToolCatalogUpdate(BaseModel):
    """``enabled`` is None to revert to registry defaults, an empty
    list to disable every tool, or an explicit name list."""

    enabled: list[str] | None = None


@router.get("/tools", response_model=ToolCatalogResponse)
async def list_tools(current_user: SuperAdmin, db: DB) -> ToolCatalogResponse:
    settings_row = await db.get(PlatformSettings, 1)
    platform_override = (
        list(settings_row.ai_tools_enabled)
        if settings_row is not None and settings_row.ai_tools_enabled is not None
        else None
    )
    effective = effective_tool_names(
        platform_enabled=platform_override,
        provider_enabled=None,
    )
    out = [
        ToolEntry(
            name=t.name,
            description=t.description,
            category=t.category,
            writes=t.writes,
            parameters_schema=t.parameters_schema(),
            default_enabled=t.default_enabled,
            enabled=t.name in effective,
        )
        for t in REGISTRY.all()
    ]
    return ToolCatalogResponse(
        tools=out,
        total=len(out),
        platform_override=platform_override,
    )


@router.put("/tools/catalog", response_model=ToolCatalogResponse)
async def update_tool_catalog(
    body: ToolCatalogUpdate,
    current_user: SuperAdmin,
    db: DB,
) -> ToolCatalogResponse:
    settings_row = await db.get(PlatformSettings, 1)
    if settings_row is None:
        settings_row = PlatformSettings(id=1)
        db.add(settings_row)
    if body.enabled is None:
        settings_row.ai_tools_enabled = None
    else:
        # Strip unknown names so a typo doesn't permanently shadow
        # a renamed tool. Same forward-compat we apply when reading.
        registered = {t.name for t in REGISTRY.all() if not t.writes}
        settings_row.ai_tools_enabled = sorted(set(body.enabled) & registered)
    await db.commit()
    return await list_tools(current_user, db)
