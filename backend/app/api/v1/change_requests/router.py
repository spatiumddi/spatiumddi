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
from datetime import datetime
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
from app.services.approvals.policy import GATEABLE_ACTIONS, GATEABLE_RESOURCE_TYPES
from app.services.approvals.service import (
    ChangeRequestStateError,
    DecisionError,
    approve_change_request,
    get_change_request,
    list_change_requests,
    mark_cancelled,
    reject_change_request,
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


def _validate_gateable(action: str | None, resource_type: str | None) -> None:
    """Reject a policy whose (action, resource_type) isn't currently wired to
    a registered risky operation (#4 — no enabled-but-inert policies).

    Only validates the fields that are present (so a PUT touching just
    ``enabled`` doesn't re-validate the unchanged action). ``"*"`` wildcard
    resource_types are P2-only (bulk ops) and rejected in P1.
    """
    if action is not None and action not in GATEABLE_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"action {action!r} is not gateable yet — supported: " f"{sorted(GATEABLE_ACTIONS)}"
            ),
        )
    if resource_type is not None and resource_type not in GATEABLE_RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"resource_type {resource_type!r} is not gateable yet — supported: "
                f"{sorted(GATEABLE_RESOURCE_TYPES)}"
            ),
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


def _can_see_all_change_requests(user: User) -> bool:
    """READ RULE (#1): who may see EVERY change request (not just their own).

    A change request row leaks the frozen args / preview_text / target /
    requester / approver. Only callers who can act on the governance surface
    should see the whole queue:

    * an **effective superadmin** (governance admin); or
    * a holder of ``{approve, change_request}`` (an eligible approver who
      needs the queue to do their job).

    Everyone else — including a plain ``{read, change_request}`` holder — sees
    only the rows they themselves requested (``requested_by_user_id ==
    caller.id``). The list endpoint forces that scope; get/{id} 404s on a row
    the caller may not see (404, not 403, so existence isn't confirmed).
    """
    return is_effective_superadmin(user) or user_has_permission(
        user, "approve", RESOURCE_TYPE_CHANGE_REQUEST
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
    caller's own requests; ``state`` / ``resource_type`` filter.

    READ RULE (#1): a caller who isn't a superadmin / approve-holder is
    ALWAYS scoped to their own requests regardless of the ``mine`` flag — a
    plain ``{read, change_request}`` holder can never enumerate the platform
    queue (which leaks args / preview / target / requester / approver)."""
    _require_read(current_user)
    if _can_see_all_change_requests(current_user):
        scope_user_id = current_user.id if mine else None
    else:
        # Force own-scope — ignore the requested ``mine`` value.
        scope_user_id = current_user.id
    rows = await list_change_requests(
        db,
        state=state,
        resource_type=resource_type,
        requested_by_user_id=scope_user_id,
        limit=max(1, min(limit, 500)),
        offset=max(0, offset),
    )
    return [_cr_to_response(r) for r in rows]


# ── Approval policies (operator-tunable rules) ─────────────────────────────


@router.get("/policies", response_model=list[ApprovalPolicyResponse])
async def list_policies(_admin: SuperAdmin, db: DB) -> list[ApprovalPolicyResponse]:
    # #2: the policy list exposes which (resource, threshold) pairs are
    # un-gated, plus the ``applies_to_superadmin`` flag — an attacker could use
    # it to learn what to delete to dodge approval. Gate on SuperAdmin, matching
    # the create/update/delete handlers (the frontend already hides the tab from
    # non-superadmins).
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
    _validate_gateable(body.action, body.resource_type)
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
    # Validate the post-update (action, resource_type) — an operator can't
    # repoint a policy at an action/type that isn't wired yet (#4).
    _validate_gateable(
        changes.get("action", policy.action),
        changes.get("resource_type", policy.resource_type),
    )
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
    # READ RULE (#1): 404 (not 403) unless the caller is the requester, holds
    # approve, or is a superadmin — so a non-eligible caller can't even confirm
    # a given change-request id EXISTS, let alone read its contents.
    if cr is None or (
        not _can_see_all_change_requests(current_user)
        and cr.requested_by_user_id != current_user.id
    ):
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

    Thin wrapper over ``services.approvals.service.approve_change_request``,
    which owns every server-side invariant (never trusting the UI) so this
    HTTP path and the AI propose→apply path enforce them identically:

    1. The row is loaded ``FOR UPDATE`` and must still be ``pending`` (a
       racing approver loses with 409); an expired row routes through the
       audited ``mark_expired`` then 409s.
    2. **Self-approval blocked** — ``approver.id != requested_by_user_id``;
       a request whose requester was deleted (``requested_by_user_id`` NULL)
       is refused (409) rather than failing open.
    3. Approver holds ``{approve, change_request}``.
    4. Approver holds the underlying operation's ``required_permission`` —
       they can't rubber-stamp a delete they couldn't perform themselves.
    5. The operation's ``preview()`` re-runs under the approver; a stale
       request (target gone / now non-empty / synthesised) does NOT execute
       (409, left ``pending`` so the requester can cancel).
    6. ``apply()`` dispatches under the **approver's** identity; the audit
       ``executed`` row carries the approver as actor and the requester id
       in ``old_value.requested_by``. A client 4xx raised by ``apply()``
       leaves the row ``pending`` and surfaces the status; a server fault
       marks it ``failed``.
    """
    note = body.decision_note if body is not None else None

    # Delegate to the single two-person spine (services/approvals/service.py)
    # so this HTTP path and the AI propose→apply path enforce the IDENTICAL
    # invariants (#5 fail-closed on a deleted requester, #11 client-4xx from
    # apply leaves the row pending, #13 expiry routes through the audited
    # mark_expired, #14 mark_failed carries the requester id, #15 guarded
    # rollback). DecisionError subclasses carry the right HTTP status.
    try:
        cr = await approve_change_request(
            db, cr_id, approver=current_user, request=request, note=note
        )
    except DecisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
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
    note = body.decision_note if body is not None else None
    try:
        cr = await reject_change_request(
            db, cr_id, approver=current_user, request=request, note=note
        )
    except DecisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
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
    # #5: pass the optional note INTO mark_cancelled so it lands on both the row
    # and the change_request.cancelled audit row (was set after the audit wrote,
    # so it never reached audit_log).
    note = body.decision_note if body is not None else None
    try:
        await mark_cancelled(db, cr, user=current_user, request=request, note=note)
    except ChangeRequestStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(cr)
    return _cr_to_response(cr)


__all__ = ["router"]
