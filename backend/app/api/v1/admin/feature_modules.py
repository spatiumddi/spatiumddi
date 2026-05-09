"""Admin REST surface for the feature-module catalog.

Two endpoints:

* ``GET  /admin/feature-modules`` — list every catalog entry alongside
  its current enabled state. Available to any authenticated user (the
  Sidebar + Cmd-K palette query this on every page so we don't gate it
  behind superadmin — viewers need to know which sections are visible
  to them too).

* ``PATCH /admin/feature-modules/{id}`` — flip the enabled state.
  Superadmin only. Writes an audit row and busts the local cache.

The response shape is intentionally flat — frontend code reads it as
``Record<string, boolean>`` for fast ``enabled[id]`` lookups, alongside
catalog metadata (label / group / description) for the Settings page.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.core.demo_mode import DEMO_RESTRICTED_MODULES, is_demo_mode
from app.models.audit import AuditLog
from app.models.feature_module import FeatureModule
from app.models.settings import PlatformSettings
from app.services import feature_modules as fm_svc

logger = structlog.get_logger(__name__)
router = APIRouter()


class FeatureModuleEntry(BaseModel):
    """Catalog entry + current state. ``label`` / ``group`` /
    ``description`` come from the in-process catalog so the UI doesn't
    need to know about it separately. ``default_enabled`` lets the
    Settings page show a "default" badge for transparency."""

    id: str
    label: str
    group: str
    description: str
    default_enabled: bool
    enabled: bool


class FeatureModuleToggleBody(BaseModel):
    enabled: bool


@router.get("/feature-modules", response_model=list[FeatureModuleEntry])
async def list_feature_modules(db: DB, current_user: CurrentUser) -> list[FeatureModuleEntry]:
    rows = (await db.execute(select(FeatureModule))).scalars().all()
    overrides: dict[str, bool] = {row.id: row.enabled for row in rows}

    return [
        FeatureModuleEntry(
            id=spec.id,
            label=spec.label,
            group=spec.group,
            description=spec.description,
            default_enabled=spec.default_enabled,
            enabled=overrides.get(spec.id, spec.default_enabled),
        )
        for spec in fm_svc.MODULES
    ]


@router.patch("/feature-modules/{module_id}", response_model=FeatureModuleEntry)
async def toggle_feature_module(
    module_id: str,
    body: FeatureModuleToggleBody,
    db: DB,
    current_user: SuperAdmin,
) -> FeatureModuleEntry:
    if not fm_svc.is_known(module_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown feature module: {module_id}",
        )

    # Demo-mode lockdown — re-enable attempts on the curated
    # abusable surface (nmap, AI, integrations) get 403'd. Disabling
    # is always allowed (operator can voluntarily turn off more).
    if is_demo_mode() and body.enabled and module_id in DEMO_RESTRICTED_MODULES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Feature '{module_id}' cannot be enabled while this "
                "instance is running in demo mode."
            ),
        )

    spec = fm_svc.MODULES_BY_ID[module_id]
    row = await fm_svc.set_module_enabled(db, module_id, body.enabled, user_id=current_user.id)
    # Mirror the toggle into the matching ``PlatformSettings`` column
    # for integrations. The Celery beat reconcilers gate on those
    # columns directly; keeping both sides in lock-step lets the
    # reconciler stop / start without touching every task. Non-
    # integration ids skip this branch.
    settings_col = fm_svc.INTEGRATION_SETTINGS_MIRROR.get(module_id)
    if settings_col is not None:
        ps = await db.get(PlatformSettings, 1)
        if ps is not None:
            setattr(ps, settings_col, body.enabled)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="feature_module",
            resource_id=module_id,
            resource_display=spec.label,
            result="success",
            new_value={"enabled": body.enabled},
        )
    )
    await db.commit()
    fm_svc.invalidate_cache()

    return FeatureModuleEntry(
        id=spec.id,
        label=spec.label,
        group=spec.group,
        description=spec.description,
        default_enabled=spec.default_enabled,
        enabled=row.enabled,
    )
