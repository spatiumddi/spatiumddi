"""Change-request lifecycle API — the two-person approval spine (#62).

This router is the operator-facing surface for the approval queue created
by ``services/approvals/gate.py``. A covered risky handler (delete_subnet,
delete_zone, …) returns ``202`` with a ``change_request`` id; the queue
sits here. A *different* eligible approver drives it to a terminal state:

* ``POST /{id}/approve`` — the two-person spine. Enforced server-side:
  approver != requester, approver holds ``{approve, change_request}``
  **and** the underlying operation's ``required_permission``. On approve
  the operation's ``preview()`` re-runs (stale-state guard); if still
  valid, ``apply()`` dispatches under the **approver's** identity and the
  audit ``executed`` row carries both user ids.
* ``POST /{id}/reject`` — an approver declines.
* ``POST /{id}/cancel`` — the requester (or a superadmin) withdraws.
* ``GET`` list / get — the queue + history.
* ``/policies`` CRUD — operator-tunable :class:`ApprovalPolicy` rows
  (superadmin only for writes).

The whole router is gated behind the ``governance.approvals`` feature
module (NN #14) via the ``require_module`` dependency on the include in
``api/v1/router.py``. Every state transition writes an ``audit_log`` row
before responding (NN #4) — owned by the service layer's ``mark_*``
helpers, committed here. Concurrency is handled with a row-level
``SELECT ... FOR UPDATE`` on the change_request plus the ``pending`` state
guard, so two racing approvers can't both execute (NN: only one wins, the
other sees 409).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, SuperAdmin
from app.core.permissions import (
    RESOURCE_TYPE_CHANGE_REQUEST,
    is_effective_superadmin,
    user_has_permission,
)
from app.core.request_meta import clean_user_agent, client_ip
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.change_request import ApprovalPolicy, ChangeRequest
from app.services.ai import operations
from app.services.approvals.service import (
    ChangeRequestStateError,
    get_change_request,
    list_change_requests,
    mark_approved,
    mark_cancelled,
    mark_executed,
    mark_failed,
    mark_rejected,
)

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────


class ChangeRequestResponse(BaseModel):
    id: uuid.UUID
    operation: str
    resource_type: str
    resource_id: str | None
    resource_display: str
    args: dict[str, Any]
    preview_text: str
    risk_reason: str
    state: str
    requested_by_user_id: uuid.UUID | None
    requested_by_display: str
    decided_by_user_id: uuid.UUID | None
    decided_by_display: str | None
    decision_note: str | None
    result: dict[str, Any] | None
    error: str | None
    expires_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None
    created_at: datetime
    modified_at: datetime


class DecisionBody(BaseModel):
    decision_note: str | None = Field(default=None, max_length=2000)


class ApprovalPolicyResponse(BaseModel):
    id: uuid.UUID
    name: str
    resource_type: str
    action: str
    min_count: int | None
    enabled: bool
    applies_to_superadmin: bool
    ttl_hours: int
    is_builtin: bool
    created_at: datetime
    modified_at: datetime


class ApprovalPolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    resource_type: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=50)
    min_count: int | None = Field(default=None, ge=1)
    enabled: bool = False
    applies_to_superadmin: bool = True
    ttl_hours: int = Field(default=168, ge=1, le=8760)


class ApprovalPolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    resource_type: str | None = Field(default=None, min_length=1, max_length=100)
    action: str | None = Field(default=None, min_length=1, max_length=50)
    min_count: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    applies_to_superadmin: bool | None = None
    ttl_hours: int | None = Field(default=None, ge=1, le=8760)


# ── Serialisers ──────────────────────────────────────────────────────────────


def _cr_to_response(cr: ChangeRequest) -> ChangeRequestResponse:
    return ChangeRequestResponse(
        id=cr.id,
        operation=cr.operation,
        resource_type=cr.resource_type,
        resource_id=cr.resource_id,
        resource_display=cr.resource_display,
        args=cr.args or {},
        preview_text=cr.preview_text,
        risk_reason=cr.risk_reason,
        state=cr.state,
        requested_by_user_id=cr.requested_by_user_id,
        requested_by_display=cr.requested_by_display,
        decided_by_user_id=cr.decided_by_user_id,
        decided_by_display=cr.decided_by_display,
        decision_note=cr.decision_note,
        result=cr.result,
        error=cr.error,
        expires_at=cr.expires_at,
        decided_at=cr.decided_at,
        executed_at=cr.executed_at,
        created_at=cr.created_at,
        modified_at=cr.modified_at,
    )


def _policy_to_response(p: ApprovalPolicy) -> ApprovalPolicyResponse:
    return ApprovalPolicyResponse(
        id=p.id,
        name=p.name,
        resource_type=p.resource_type,
        action=p.action,
        min_count=p.min_count,
        enabled=p.enabled,
        applies_to_superadmin=p.applies_to_superadmin,
        ttl_hours=p.ttl_hours,
        is_builtin=p.is_builtin,
        created_at=p.created_at,
        modified_at=p.modified_at,
    )


def _audit_policy(
    db: DB,
    *,
    user: User,
    request: Request,
    action: str,
    policy_id: str,
    name: str,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Append an ``audit_log`` row for an ``approval_policy`` mutation (NN #4)."""
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=user.auth_source,
            source_ip=client_ip(request),
            user_agent=clean_user_agent(request.headers.get("user-agent")),
            action=action,
            resource_type="approval_policy",
            resource_id=policy_id,
            resource_display=name,
            new_value=new_value,
        )
    )


def _require_read(user: CurrentUser) -> None:
    """Reads on the queue need ``read,change_request`` (or superadmin)."""
    if is_effective_superadmin(user):
        return
    if not user_has_permission(user, "read", RESOURCE_TYPE_CHANGE_REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: need 'read' on '{RESOURCE_TYPE_CHANGE_REQUEST}'",
        )


# ── Change-request queue ───────────────────────────────────────────────────


@router.get("", response_model=list[ChangeRequestResponse])
async def list_requests(
    current_user: CurrentUser,
    db: DB,
    state: str | None = None,
    resource_type: str | None = None,
    mine: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[ChangeRequestResponse]:
    """List change requests newest-first. ``mine=true`` scopes to the
    caller's own requests; ``state`` / ``resource_type`` filter."""
    _require_read(current_user)
    rows = await list_change_requests(
        db,
        state=state,
        resource_type=resource_type,
        requested_by_user_id=current_user.id if mine else None,
        limit=max(1, min(limit, 500)),
        offset=max(0, offset),
    )
    return [_cr_to_response(r) for r in rows]


# ── Approval policies (operator-tunable rules) ─────────────────────────────


@router.get("/policies", response_model=list[ApprovalPolicyResponse])
async def list_policies(current_user: CurrentUser, db: DB) -> list[ApprovalPolicyResponse]:
    _require_read(current_user)
    rows = (
        (await db.execute(select(ApprovalPolicy).order_by(ApprovalPolicy.name.asc())))
        .scalars()
        .all()
    )
    return [_policy_to_response(p) for p in rows]


@router.post(
    "/policies",
    response_model=ApprovalPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_policy(
    body: ApprovalPolicyCreate,
    _admin: SuperAdmin,
    db: DB,
    request: Request,
) -> ApprovalPolicyResponse:
    policy = ApprovalPolicy(
        name=body.name,
        resource_type=body.resource_type,
        action=body.action,
        min_count=body.min_count,
        enabled=body.enabled,
        applies_to_superadmin=body.applies_to_superadmin,
        ttl_hours=body.ttl_hours,
        is_builtin=False,
    )
    db.add(policy)
    await db.flush()
    _audit_policy(
        db,
        user=_admin,
        request=request,
        action="write",
        policy_id=str(policy.id),
        name=policy.name,
        new_value={
            "resource_type": policy.resource_type,
            "action": policy.action,
            "min_count": policy.min_count,
            "enabled": policy.enabled,
        },
    )
    await db.commit()
    await db.refresh(policy)
    return _policy_to_response(policy)


@router.put("/policies/{policy_id}", response_model=ApprovalPolicyResponse)
async def update_policy(
    policy_id: uuid.UUID,
    body: ApprovalPolicyUpdate,
    _admin: SuperAdmin,
    db: DB,
    request: Request,
) -> ApprovalPolicyResponse:
    policy = await db.get(ApprovalPolicy, policy_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(policy, field, value)
    _audit_policy(
        db,
        user=_admin,
        request=request,
        action="write",
        policy_id=str(policy.id),
        name=policy.name,
        new_value=changes,
    )
    await db.commit()
    await db.refresh(policy)
    return _policy_to_response(policy)


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID,
    _admin: SuperAdmin,
    db: DB,
    request: Request,
) -> None:
    policy = await db.get(ApprovalPolicy, policy_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if policy.is_builtin:
        # Built-in policies are kept for the registry-completeness check;
        # toggle ``enabled`` off instead of deleting them.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Built-in policies cannot be deleted — disable them instead",
        )
    name = policy.name
    await db.delete(policy)
    _audit_policy(
        db,
        user=_admin,
        request=request,
        action="delete",
        policy_id=str(policy_id),
        name=name,
    )
    await db.commit()
    return None


@router.get("/{cr_id}", response_model=ChangeRequestResponse)
async def get_request(cr_id: uuid.UUID, current_user: CurrentUser, db: DB) -> ChangeRequestResponse:
    _require_read(current_user)
    cr = await get_change_request(db, cr_id)
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _cr_to_response(cr)


@router.post("/{cr_id}/approve", response_model=ChangeRequestResponse)
async def approve_request(
    cr_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    request: Request,
    body: DecisionBody | None = None,
) -> ChangeRequestResponse:
    """Approve and execute a pending change request — the two-person spine.

    Server-side invariants (every one enforced here, never trusting the UI):

    1. The row is loaded ``FOR UPDATE`` and must still be ``pending`` (a
       racing approver loses with 409); an expired row flips to ``expired``.
    2. **Self-approval blocked** — ``approver.id != requested_by_user_id``.
    3. Approver holds ``{approve, change_request}``.
    4. Approver holds the underlying operation's ``required_permission`` —
       they can't rubber-stamp a delete they couldn't perform themselves.
    5. The operation's ``preview()`` re-runs under the approver; a stale
       request (target gone / now non-empty / synthesised) does NOT execute
       (409, left ``pending`` so the requester can cancel).
    6. ``apply()`` dispatches under the **approver's** identity; the audit
       ``executed`` row carries the approver as actor and the requester id
       in ``old_value.requested_by``.
    """
    note = body.decision_note if body is not None else None

    # 1. Row lock + state guard — race-safe against a second approver.
    cr = await get_change_request(db, cr_id, for_update=True)
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if cr.state != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Change request is not pending (state={cr.state!r})",
        )
    if cr.expires_at < datetime.now(UTC):
        cr.state = "expired"
        cr.decided_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Change request has expired",
        )

    # 2. Self-approval is forbidden — the whole point of two-person.
    if cr.requested_by_user_id is not None and current_user.id == cr.requested_by_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot approve your own change request",
        )

    op = operations.get_operation(cr.operation)
    if op is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Operation {cr.operation!r} is not registered",
        )

    # 3. Approver holds the approve capability.
    if not user_has_permission(current_user, "approve", RESOURCE_TYPE_CHANGE_REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: need 'approve' on '{RESOURCE_TYPE_CHANGE_REQUEST}'",
        )

    # 4. Approver holds the underlying operation's own permission.
    try:
        operations.enforce_operation_permission(current_user, op)
    except operations.OperationPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    # Re-validate the frozen args through the op's pydantic model — guards
    # the rare redeploy-mid-flight schema-change case.
    try:
        args = op.args_model.model_validate(cr.args or {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Stored args no longer validate: {exc}",
        ) from exc

    # 5. Stale-state re-check. Leave the row pending (requester may cancel)
    #    but surface the diagnostic — do NOT execute a doomed op.
    preview = await op.preview(db, current_user, args)
    if not preview.ok:
        logger.info(
            "change_request.stale",
            change_request_id=str(cr.id),
            operation=cr.operation,
            detail=preview.detail,
            approver=str(current_user.id),
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=preview.detail)

    requester_id = cr.requested_by_user_id

    # Record the approval transition (pending → approved, audited).
    await mark_approved(db, cr, approver=current_user, request=request, note=note)

    # 6. Dispatch apply() under the approver's identity. ``apply`` runs its
    #    own mutation + writes its own audit row(s); on success mark_executed
    #    writes the change_request.executed audit row carrying both ids.
    try:
        result = await op.apply(db, current_user, args)
    except operations.OperationPermissionError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # apply() rolled back its own transaction; reload + lock the row to
        # stamp the failure (the approval transition was rolled back too).
        await db.rollback()
        cr = await get_change_request(db, cr_id, for_update=True)
        if cr is None or cr.state != "pending":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Change request vanished or changed state mid-apply — investigate",
            ) from exc
        await mark_approved(db, cr, approver=current_user, request=request, note=note)
        await mark_failed(db, cr, approver=current_user, request=request, error=str(exc))
        await db.commit()
        logger.warning(
            "change_request.apply_failed",
            change_request_id=str(cr.id),
            operation=cr.operation,
            error=str(exc),
        )
        return _cr_to_response(cr)

    await mark_executed(
        db,
        cr,
        approver=current_user,
        requester_id=requester_id,
        request=request,
        result=result,
    )
    await db.commit()
    await db.refresh(cr)
    return _cr_to_response(cr)


@router.post("/{cr_id}/reject", response_model=ChangeRequestResponse)
async def reject_request(
    cr_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    request: Request,
    body: DecisionBody | None = None,
) -> ChangeRequestResponse:
    """Decline a pending request. Needs ``approve,change_request``; like
    approve, the rejecter may not be the requester (a self-reject is just a
    cancel — use that)."""
    if not user_has_permission(current_user, "approve", RESOURCE_TYPE_CHANGE_REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: need 'approve' on '{RESOURCE_TYPE_CHANGE_REQUEST}'",
        )
    cr = await get_change_request(db, cr_id, for_update=True)
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if cr.requested_by_user_id is not None and current_user.id == cr.requested_by_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot reject your own request — cancel it instead",
        )
    note = body.decision_note if body is not None else None
    try:
        await mark_rejected(db, cr, approver=current_user, request=request, note=note)
    except ChangeRequestStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(cr)
    return _cr_to_response(cr)


@router.post("/{cr_id}/cancel", response_model=ChangeRequestResponse)
async def cancel_request(
    cr_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    request: Request,
    body: DecisionBody | None = None,
) -> ChangeRequestResponse:
    """Withdraw a pending request. Only the original requester or a
    superadmin may cancel."""
    cr = await get_change_request(db, cr_id, for_update=True)
    if cr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    is_requester = (
        cr.requested_by_user_id is not None and current_user.id == cr.requested_by_user_id
    )
    if not is_requester and not is_effective_superadmin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the requester or a superadmin can cancel this request",
        )
    try:
        await mark_cancelled(db, cr, user=current_user, request=request)
    except ChangeRequestStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # Carry the optional note even though cancel doesn't require one.
    if body is not None and body.decision_note is not None:
        cr.decision_note = body.decision_note
    await db.commit()
    await db.refresh(cr)
    return _cr_to_response(cr)


__all__ = ["router"]
