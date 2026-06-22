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

import asyncio
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.core.demo_mode import DEMO_RESTRICTED_MODULES, is_demo_mode
from app.core.permissions import is_effective_superadmin
from app.models.audit import AuditLog
from app.models.feature_module import FeatureModule
from app.models.settings import PlatformSettings
from app.services import feature_modules as fm_svc
from app.services.ai.operations import get_operation
from app.services.ai.operations_control import (
    MODULE_ID as APPROVALS_MODULE_ID,
)
from app.services.ai.operations_control import (
    ControlKind,
    ModifyApprovalControlArgs,
    is_controls_protected,
    maybe_gate_control,
)
from app.services.reauth import ReauthOutcome, reverify_operator

logger = structlog.get_logger(__name__)
router = APIRouter()

# Typed confirmation phrase the break-glass caller must echo exactly — mirrors
# factory-reset's exact-match phrase guard. Wrong phrase → 422.
BREAK_GLASS_PHRASE = "BREAK GLASS"


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
    # #62: when enabling ``governance.approvals``, the operator may also turn
    # on the self-governance lock in the same call. Only honoured for that
    # module on the enable path; ignored everywhere else (enabling never
    # CLEARS the lock — that's a gated weakening move).
    protect_controls: bool | None = None


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


@router.patch("/feature-modules/{module_id}")
async def toggle_feature_module(
    module_id: str,
    body: FeatureModuleToggleBody,
    db: DB,
    current_user: SuperAdmin,
    request: Request,
):  # noqa: ANN201 — returns FeatureModuleEntry OR a 202 JSONResponse (gated)
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

    # #62 self-governance lock: disabling ``governance.approvals`` is a
    # WEAKENING change. When the lock is on (and the caller isn't going
    # through break-glass), this returns a 202 + a pending change_request a
    # SECOND superadmin must approve, INSTEAD of disabling inline. When the
    # lock is off ``maybe_gate_control`` returns None immediately (before any
    # extra query) so the inline path below is byte-identical to today.
    if module_id == APPROVALS_MODULE_ID and body.enabled is False:
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        pending = await maybe_gate_control(
            db,
            current_user,
            request,
            kind="disable_module",
            resource_type="feature_module",
            resource_id=module_id,
            resource_display="Approval workflows",
        )
        if pending is not None:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=pending.as_dict(),
            )

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

    # #62 set-at-enable-time: turning ON ``governance.approvals`` with
    # ``protect_controls=true`` also flips the self-protection lock on. This
    # is a STRENGTHENING move → stays single-person inline + audited.
    # ``protect_controls`` is never honoured to CLEAR the lock here (that's a
    # gated weakening move via break-glass / approval).
    if module_id == APPROVALS_MODULE_ID and body.enabled and body.protect_controls:
        ps = await db.get(PlatformSettings, 1)
        if ps is not None and not ps.approvals_protect_controls:
            ps.approvals_protect_controls = True
            db.add(
                AuditLog(
                    user_id=current_user.id,
                    user_display_name=current_user.display_name,
                    auth_source=current_user.auth_source,
                    action="update",
                    resource_type="platform_settings",
                    resource_id="approvals_protect_controls",
                    resource_display="Approval self-protection lock",
                    result="success",
                    new_value={"approvals_protect_controls": True},
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


# ── Self-protection lock toggle (#62) ───────────────────────────────────


class ApprovalsLockBody(BaseModel):
    enabled: bool


@router.get("/feature-modules/approvals-lock")
async def get_approvals_lock(db: DB, current_user: SuperAdmin) -> dict:
    """Current self-governance lock state (superadmin)."""
    return {"approvals_protect_controls": await is_controls_protected(db)}


@router.post("/feature-modules/approvals-lock")
async def set_approvals_lock(
    body: ApprovalsLockBody,
    db: DB,
    current_user: SuperAdmin,
    request: Request,
):  # noqa: ANN201 — {state} OR a 202 JSONResponse (gated when turning OFF)
    """Turn the self-governance lock on / off (superadmin, #62).

    Turning it ON is a STRENGTHENING move → inline + audited, single-person.
    Turning it OFF is a WEAKENING move → when the lock is currently on it
    routes through ``maybe_gate_control`` (a second superadmin must approve)
    so you can't quietly self-unlock; the break-glass endpoint is the
    immediate escape hatch.
    """
    currently = await is_controls_protected(db)

    if body.enabled:
        # Strengthen — inline.
        if not currently:
            ps = await db.get(PlatformSettings, 1)
            if ps is not None:
                ps.approvals_protect_controls = True
            db.add(
                AuditLog(
                    user_id=current_user.id,
                    user_display_name=current_user.display_name,
                    auth_source=current_user.auth_source,
                    action="update",
                    resource_type="platform_settings",
                    resource_id="approvals_protect_controls",
                    resource_display="Approval self-protection lock",
                    result="success",
                    new_value={"approvals_protect_controls": True},
                )
            )
            await db.commit()
        return {"approvals_protect_controls": True}

    # Weaken (turn off) — gate when currently on.
    pending = await maybe_gate_control(
        db,
        current_user,
        request,
        kind="unlock",
        resource_type="platform_settings",
        resource_id="approvals_protect_controls",
        resource_display="Approval self-protection lock",
    )
    if pending is not None:
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=pending.as_dict())

    # Lock already off (maybe_gate_control short-circuited) → idempotent no-op.
    return {"approvals_protect_controls": False}


# ── Break-glass: force a protected control change immediately (#62) ──────


class BreakGlassBody(BaseModel):
    """Force a weakening control change immediately, bypassing the
    two-person gate. The mandatory anti-lockout escape hatch."""

    kind: ControlKind
    policy_id: UUID | None = None
    # Re-confirmation — local users supply ``password``; external-auth
    # users (no local password) supply ``totp_code`` (see services/reauth.py).
    password: str | None = None
    totp_code: str | None = None
    # Typed confirmation phrase — must equal BREAK_GLASS_PHRASE exactly.
    confirm_phrase: str = Field(min_length=1)


def _break_glass_audit(
    db: DB,
    *,
    user,  # type: ignore[no-untyped-def]
    kind: str,
    result: str,
    new_value: dict | None = None,
) -> None:
    """HIGH-severity break-glass audit row.

    There is no ``severity`` column (NN #4 trail is the signal); the distinct
    ``action="approvals.break_glass"`` + the ``governance.break_glass`` typed
    event (event_publisher special map) convey the high-severity intent so
    operators can wire a dedicated alert / SIEM rule on it.
    """
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=getattr(user, "auth_source", "local") or "local",
            action="approvals.break_glass",
            resource_type="approval_control",
            resource_id=kind,
            resource_display=f"Break-glass: {kind}",
            result=result,
            new_value=new_value,
        )
    )


@router.post("/feature-modules/break-glass", status_code=status.HTTP_200_OK)
async def break_glass(
    body: BreakGlassBody,
    db: DB,
    current_user: SuperAdmin,
    request: Request,
) -> dict:
    """Force a protected control change IMMEDIATELY (anti-lockout, #62).

    Superadmin-only. Requires BOTH a typed confirmation phrase AND password /
    TOTP re-confirmation, then executes the weakening control change under the
    calling superadmin — bypassing the two-person gate. This must never be
    gateable itself, or a fully-locked platform with no second superadmin could
    be permanently wedged. Writes a HIGH-severity audit row + fires the
    ``governance.break_glass`` event.
    """
    # 1. Superadmin gate (SuperAdmin dep already enforces, but audit the shape).
    if not is_effective_superadmin(current_user):  # pragma: no cover — dep enforces
        _break_glass_audit(
            db,
            user=current_user,
            kind=body.kind,
            result="forbidden",
            new_value={"reason": "non_superadmin"},
        )
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only superadmins can break the glass")

    # 2. Typed confirmation phrase — exact match (mirrors factory-reset).
    if body.confirm_phrase != BREAK_GLASS_PHRASE:
        _break_glass_audit(
            db,
            user=current_user,
            kind=body.kind,
            result="forbidden",
            new_value={"reason": "bad_phrase"},
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Confirmation phrase must be exactly {BREAK_GLASS_PHRASE!r}",
        )

    # 3. Password / TOTP re-confirmation (#408 pattern).
    outcome = reverify_operator(current_user, password=body.password, totp_code=body.totp_code)
    if outcome is not ReauthOutcome.OK:
        reason = "mfa_required" if outcome is ReauthOutcome.MFA_REQUIRED else "bad_credential"
        _break_glass_audit(
            db,
            user=current_user,
            kind=body.kind,
            result="forbidden",
            new_value={"reason": reason},
        )
        await db.commit()
        # Friction-sleep so a bad-credential attempt doesn't leak via timing.
        await asyncio.sleep(0.5)
        if outcome is ReauthOutcome.MFA_REQUIRED:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Re-confirmation requires MFA. Your account has no local "
                "password — enrol TOTP under Settings → Security, then retry.",
            )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password or TOTP code is incorrect")

    # 4. Execute the control change IMMEDIATELY under the caller, bypassing the
    #    gate (apply() never re-enters maybe_gate_control). Write the
    #    HIGH-severity audit row in the same transaction BEFORE apply commits
    #    it, so the break-glass is recorded even if apply's own audit + commit
    #    is the thing that lands. apply() commits; we add our row first.
    op = get_operation("modify_approval_control")
    assert op is not None  # registered at import
    args = ModifyApprovalControlArgs(kind=body.kind, policy_id=body.policy_id)

    # Stale-state guard — refuse a moot change with a clear reason.
    preview = await op.preview(db, current_user, args)
    if not preview.ok:
        _break_glass_audit(
            db,
            user=current_user,
            kind=body.kind,
            result="error",
            new_value={"reason": "stale", "detail": preview.detail},
        )
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, preview.detail)

    _break_glass_audit(
        db,
        user=current_user,
        kind=body.kind,
        result="success",
        new_value={"kind": body.kind, "policy_id": str(body.policy_id) if body.policy_id else None},
    )
    logger.warning(
        "approvals.break_glass",
        user=current_user.username,
        kind=body.kind,
        policy_id=str(body.policy_id) if body.policy_id else None,
    )
    # apply() runs the mutation + its own audit row + commit (our break-glass
    # row is flushed in the same session and lands on that commit).
    result = await op.apply(db, current_user, args)
    return {"forced": True, "kind": body.kind, "result": result}
