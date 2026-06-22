"""Self-governance control-plane operations + flag gate (#62).

The approval *control plane* — the ``governance.approvals`` module toggle,
the ``approval_policy`` rows, and the ``approvals_protect_controls`` lock —
can itself be weakened. A single rogue / compromised superadmin could
disable the module, delete every policy, or flip the lock off and then
delete whatever they wanted with no second pair of eyes.

The self-governance lock closes that hole. It is OPT-IN
(``PlatformSettings.approvals_protect_controls``, set at module-enable
time). When ON, every *weakening* change to the control plane is itself
gated behind a SECOND superadmin's approval, reusing the same
``change_request`` flow risky deletes use:

  * disabling the ``governance.approvals`` module        (kind=disable_module)
  * disabling a policy (enabled true → false)            (kind=disable_policy)
  * deleting a policy                                    (kind=delete_policy)
  * lowering a policy's superadmin gate (true → false)   (kind=lower_superadmin_gate)
  * turning the lock itself off (true → false)           (kind=unlock)

STRENGTHENING moves (enabling the module, enabling a policy, raising the
superadmin gate, turning the lock on) stay single-person inline — making
the control safer never needs a second person.

These ops are NOT policy-row driven (unlike the risky deletes that flow
through ``match_policy``). They are gated DIRECTLY on the flag, so they
must NOT appear in ``RISKY_OPERATION_NAMES`` (which is asserted against the
``GATEABLE_*`` policy-key sets at boot) and their ``required_permission``
pair (``("admin", "approval_control")``) must NOT be added to
``GATEABLE_ACTIONS`` / ``GATEABLE_RESOURCE_TYPES``. ``CONTROL_OPERATION_NAMES``
below is the separate registry the approve spine consults to enforce the
superadmin-only requirement on top of the normal checks.

BREAK-GLASS — a superadmin can force any of these weakening changes
IMMEDIATELY (bypassing the two-person gate) via the dedicated break-glass
endpoint, which re-confirms the operator's password / TOTP AND a typed
confirmation phrase and writes a HIGH-severity audit row + event. That is
the mandatory anti-lockout escape hatch; it calls ``apply()`` directly and
never re-enters the gate.

CLAUDE.md non-negotiables honoured: #2 (async throughout), #3
(server-side authorization — superadmin enforced in the approve spine + on
the break-glass endpoint), #4 (each ``apply()`` writes its own audit row
before commit).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.change_request import ApprovalPolicy
from app.models.settings import PlatformSettings
from app.services.ai.operations import (
    Operation,
    PreviewResult,
    get_operation,
    register,
)

logger = structlog.get_logger(__name__)

MODULE_ID = "governance.approvals"

# The control operation's RBAC pair. Deliberately NOT in GATEABLE_ACTIONS /
# GATEABLE_RESOURCE_TYPES — these ops are flag-gated, not policy-row gated,
# so ``match_policy`` must never try to gate them and the gate-invariant
# assertion must never see them (they're absent from RISKY_OPERATION_NAMES).
_CONTROL_PERMISSION: tuple[str, str] = ("admin", "approval_control")

# Human kind → preview verb. Single source of truth for both preview() and
# the break-glass audit ``resource_display``.
_KIND_LABELS: dict[str, str] = {
    "disable_module": "Disable approval workflows entirely",
    "disable_policy": "Disable approval policy",
    "delete_policy": "Delete approval policy",
    "lower_superadmin_gate": "Stop a policy applying to superadmins",
    "unlock": "Remove the require-approval-to-disable lock",
}

ControlKind = Literal[
    "disable_module",
    "disable_policy",
    "delete_policy",
    "lower_superadmin_gate",
    "unlock",
]


class ModifyApprovalControlArgs(BaseModel):
    """Args for the ``modify_approval_control`` operation.

    ``kind`` discriminates the five weakening changes. ``policy_id`` is
    required for the three policy-scoped kinds; ignored for
    ``disable_module`` / ``unlock``.
    """

    kind: ControlKind
    policy_id: UUID | None = None


async def is_controls_protected(db: AsyncSession) -> bool:
    """True iff the self-governance lock is ON.

    The single read all three gated surfaces share so they observe the
    flag identically. Missing settings row → not protected (defensive: a
    fresh install with no row behaves exactly as today)."""
    ps = await db.get(PlatformSettings, 1)
    return bool(ps is not None and ps.approvals_protect_controls)


# ── modify_approval_control operation ───────────────────────────────────


async def _preview_modify_approval_control(
    db: AsyncSession, user: User, args: ModifyApprovalControlArgs
) -> PreviewResult:
    """Re-validate the weakening change + render a clear human description.

    ``ok=False`` (with a reason) when the change is already moot — the
    module is already off, the policy vanished or is already disabled, the
    lock is already off — so the approve spine refuses to execute a no-op /
    doomed control change exactly like a stale risky delete.
    """
    from app.services.feature_modules import is_module_enabled  # noqa: PLC0415

    kind = args.kind

    if kind == "disable_module":
        if not await is_module_enabled(db, MODULE_ID):
            return PreviewResult(ok=False, detail="Approval workflows are already disabled.")
        return PreviewResult(
            ok=True, detail="ready", preview_text="Disable approval workflows entirely"
        )

    if kind == "unlock":
        if not await is_controls_protected(db):
            return PreviewResult(
                ok=False, detail="The require-approval-to-disable lock is already off."
            )
        return PreviewResult(
            ok=True,
            detail="ready",
            preview_text="Remove the require-approval-to-disable lock",
        )

    # Policy-scoped kinds need the row.
    if args.policy_id is None:
        return PreviewResult(ok=False, detail=f"{kind} requires a policy_id.")
    policy = await db.get(ApprovalPolicy, args.policy_id)
    if policy is None:
        return PreviewResult(ok=False, detail=f"Approval policy {args.policy_id} not found.")

    if kind == "disable_policy":
        if not policy.enabled:
            return PreviewResult(ok=False, detail=f"Policy {policy.name!r} is already disabled.")
        return PreviewResult(
            ok=True, detail="ready", preview_text=f"Disable approval policy `{policy.name}`"
        )
    if kind == "delete_policy":
        if policy.is_builtin:
            return PreviewResult(
                ok=False,
                detail="Built-in policies cannot be deleted — disable them instead.",
            )
        return PreviewResult(
            ok=True, detail="ready", preview_text=f"Delete approval policy `{policy.name}`"
        )
    if kind == "lower_superadmin_gate":
        if not policy.applies_to_superadmin:
            return PreviewResult(
                ok=False,
                detail=f"Policy {policy.name!r} already does not apply to superadmins.",
            )
        return PreviewResult(
            ok=True,
            detail="ready",
            preview_text=f"Stop policy `{policy.name}` applying to superadmins",
        )

    return PreviewResult(ok=False, detail=f"Unknown control kind {kind!r}.")  # pragma: no cover


def _control_audit(
    db: AsyncSession,
    *,
    user: User,
    action: str,
    resource_type: str,
    resource_id: str,
    resource_display: str,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Append an audit row for a control-plane mutation (NN #4)."""
    from app.models.audit import AuditLog  # noqa: PLC0415

    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=getattr(user, "auth_source", "local") or "local",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_display=resource_display,
            result="success",
            new_value=new_value,
        )
    )


async def _apply_modify_approval_control(
    db: AsyncSession, user: User, args: ModifyApprovalControlArgs
) -> dict[str, Any]:
    """Perform the weakening control change UNDER THE APPROVER + audit it.

    Called by the approve spine (second superadmin) AND by the break-glass
    endpoint (the same superadmin, after re-confirm). Each branch reads the
    relevant state FRESH and is idempotent against a state that already
    moved — so a break-glass ``unlock`` after a quiet flip is a no-op, not a
    crash. Commits on success.
    """
    from app.services.feature_modules import (  # noqa: PLC0415
        invalidate_cache,
        set_module_enabled,
    )

    kind = args.kind

    if kind == "disable_module":
        # NOTE: this is the disable path itself — set_module_enabled writes
        # the override row; we do NOT re-enter the gate (apply() never calls
        # gate_or_execute), so the break-glass / approve disable can't
        # self-block.
        await set_module_enabled(db, MODULE_ID, False, user_id=user.id)
        _control_audit(
            db,
            user=user,
            action="update",
            resource_type="feature_module",
            resource_id=MODULE_ID,
            resource_display="Approval workflows",
            new_value={"enabled": False, "via": "approval_control"},
        )
        await db.commit()
        invalidate_cache()
        return {"kind": kind, "module": MODULE_ID, "enabled": False}

    if kind == "unlock":
        ps = await db.get(PlatformSettings, 1)
        if ps is not None:
            ps.approvals_protect_controls = False
        _control_audit(
            db,
            user=user,
            action="update",
            resource_type="platform_settings",
            resource_id="approvals_protect_controls",
            resource_display="Approval self-protection lock",
            new_value={"approvals_protect_controls": False, "via": "approval_control"},
        )
        await db.commit()
        return {"kind": kind, "approvals_protect_controls": False}

    # Policy-scoped kinds.
    if args.policy_id is None:
        raise ValueError(f"{kind} requires a policy_id.")
    policy = await db.get(ApprovalPolicy, args.policy_id)
    if policy is None:
        raise ValueError(f"Approval policy {args.policy_id} not found.")
    name = policy.name

    if kind == "disable_policy":
        policy.enabled = False
        new_value: dict[str, Any] = {"enabled": False, "via": "approval_control"}
        action_audit = "write"
    elif kind == "lower_superadmin_gate":
        policy.applies_to_superadmin = False
        new_value = {"applies_to_superadmin": False, "via": "approval_control"}
        action_audit = "write"
    elif kind == "delete_policy":
        if policy.is_builtin:
            raise ValueError("Built-in policies cannot be deleted — disable them instead.")
        await db.delete(policy)
        new_value = {"deleted": True, "via": "approval_control"}
        action_audit = "delete"
    else:  # pragma: no cover — args model constrains kind
        raise ValueError(f"Unknown control kind {kind!r}.")

    _control_audit(
        db,
        user=user,
        action=action_audit,
        resource_type="approval_policy",
        resource_id=str(args.policy_id),
        resource_display=name,
        new_value=new_value,
    )
    await db.commit()
    return {"kind": kind, "policy_id": str(args.policy_id)}


_OP_MODIFY_APPROVAL_CONTROL = Operation(
    name="modify_approval_control",
    description=(
        "Weaken the approval control plane (disable the module, disable / "
        "delete a policy, lower a policy's superadmin gate, or remove the "
        "self-protection lock). Gated behind a second superadmin's approval "
        "when the self-protection lock is on."
    ),
    args_model=ModifyApprovalControlArgs,
    preview=_preview_modify_approval_control,
    apply=_apply_modify_approval_control,
    category="governance",
    required_permission=_CONTROL_PERMISSION,
)
register(_OP_MODIFY_APPROVAL_CONTROL)


# The approve spine consults this to enforce the superadmin-only requirement
# on a control op IN ADDITION to the normal {approve, change_request} +
# required_permission checks. Kept separate from RISKY_OPERATION_NAMES so the
# gate-invariant assertion (which asserts those against GATEABLE_*) never sees
# these flag-gated ops.
CONTROL_OPERATION_NAMES: frozenset[str] = frozenset({"modify_approval_control"})


def _assert_registered() -> None:  # pragma: no cover — import-time invariant
    if get_operation("modify_approval_control") is None:
        raise RuntimeError("modify_approval_control operation not registered")


_assert_registered()


# ── Flag-driven gate the three control surfaces call ─────────────────────


async def maybe_gate_control(
    db: AsyncSession,
    user: User,
    request: Request,
    *,
    kind: ControlKind,
    policy_id: UUID | None = None,
    resource_type: str,
    resource_id: str | None,
    resource_display: str,
) -> Any:
    """Decide whether a weakening control change must be queued for a second
    superadmin (the flag-driven analogue of ``gate_or_execute``).

    Returns ``None`` (proceed INLINE, single-person, byte-identical to
    pre-#62) the instant the lock is OFF — before any other query — so the
    flag-off path never even constructs a change_request. When the lock is
    ON, re-runs the op's ``preview()`` (409 if the change is already moot)
    and persists a pending ``change_request`` for ``modify_approval_control``,
    returning a :class:`ChangeRequestPending`. The break-glass endpoint does
    NOT call this — it calls ``apply()`` directly.
    """
    # Local imports — keep the approvals package off this module's import
    # graph (operations_control is imported by the API layer at startup).
    from app.services.approvals.gate import ChangeRequestPending  # noqa: PLC0415
    from app.services.approvals.service import create_change_request  # noqa: PLC0415

    # 1. Lock off → never gate, never query anything. Flag-off MUST be
    #    byte-identical to today (#62 design footgun guard).
    if not await is_controls_protected(db):
        return None

    op = get_operation("modify_approval_control")
    assert op is not None  # registered at import
    args = ModifyApprovalControlArgs(kind=kind, policy_id=policy_id)

    # 2. Re-run preview — don't queue a doomed / no-op control change.
    from fastapi import HTTPException, status  # noqa: PLC0415

    preview = await op.preview(db, user, args)
    if not preview.ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=preview.detail)

    cr = await create_change_request(
        db,
        user=user,
        request=request,
        operation=op.name,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_display=resource_display,
        args=args.model_dump(mode="json"),
        preview_text=preview.preview_text,
        risk_reason="approvals self-protection lock",
        ttl_hours=168,
    )
    await db.commit()
    logger.info(
        "approval_control.queued",
        change_request_id=str(cr.id),
        kind=kind,
        requested_by=str(user.id),
    )
    return ChangeRequestPending(
        change_request_id=cr.id, state="pending", preview_text=cr.preview_text
    )


__all__ = [
    "CONTROL_OPERATION_NAMES",
    "ControlKind",
    "ModifyApprovalControlArgs",
    "is_controls_protected",
    "maybe_gate_control",
]
