"""Self-service request portal API (issue #696).

The #62 approval engine as a *brake* gates risky deletes. This router is the
same machinery pointed the other way: a low-privilege user asks for something
they cannot do themselves, an approver reviews it with the operation's own
preview in front of them, and approving **executes the provisioning**.

* ``GET  /catalog`` — what may be asked for (the allow-list + arg schemas).
* ``POST /`` — submit a request. Needs ``create,provisioning_request``; does
  **not** need the underlying operation's permission, which is the entire
  point (see ``services/requests/service.py``).
* ``GET  /`` list, ``GET /{id}`` — the queue. Same read rule as #62: a caller
  who cannot approve sees only their own rows, and an unreadable id 404s
  rather than 403s so existence is not confirmed.
* ``POST /{id}/approve|reject|cancel`` — thin delegations to the **shared**
  ``services.approvals.service`` spine. Every server-side invariant lives
  there: FOR UPDATE + pending guard, fail-closed on a deleted requester,
  self-approval block, approver holds ``{approve, change_request}`` AND the
  operation's ``required_permission``, re-preview stale guard, ``apply()``
  under the approver's identity, audit rows on every transition.

There is deliberately no second state machine here (#696), and no provisioning
code — the catalog maps each kind onto an ``Operation`` that already calls the
same service-layer functions the REST handlers do.

Gated behind the ``governance.requests`` feature module (NN #14) on the
include in ``api/v1/router.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.core.permissions import (
    RESOURCE_TYPE_CHANGE_REQUEST,
    RESOURCE_TYPE_PROVISIONING_REQUEST,
    is_effective_superadmin,
    user_has_permission,
)
from app.models.auth import User
from app.models.change_request import ChangeRequest
from app.services.approvals.service import (
    ChangeRequestStateError,
    DecisionError,
    approve_change_request,
    get_change_request,
    list_change_requests,
    mark_cancelled,
    reject_change_request,
)
from app.services.requests.catalog import all_kinds, resolve_operation
from app.services.requests.service import MAX_JUSTIFICATION_CHARS, submit_request

router = APIRouter()

_ORIGIN = "portal"


# ── Schemas ────────────────────────────────────────────────────────────────


class RequestKindResponse(BaseModel):
    kind: str
    label: str
    description: str
    category: str
    operation: str
    # JSON Schema of the operation's args model, so the form can be rendered
    # (and validated client-side) from the same source the server validates
    # against — there is no second schema to drift.
    args_schema: dict[str, Any]


class SubmitRequestBody(BaseModel):
    kind: str = Field(..., description="Catalog kind id, e.g. 'dns_record'")
    args: dict[str, Any] = Field(default_factory=dict)
    # No ``max_length`` here on purpose — the bound is enforced in
    # ``submit_request`` so every caller hits the same rule and gets the same
    # readable 400 rather than a schema 422 from one path and a 400 from
    # another. MAX_JUSTIFICATION_CHARS is still referenced in the description
    # so the OpenAPI schema documents the limit.
    justification: str | None = Field(
        default=None,
        description=(
            "Why the requester needs this; shown to the approver. "
            f"Max {MAX_JUSTIFICATION_CHARS} characters."
        ),
    )


class DecisionBody(BaseModel):
    decision_note: str | None = Field(default=None, max_length=2000)


class ProvisioningRequestResponse(BaseModel):
    id: uuid.UUID
    operation: str
    resource_type: str
    resource_display: str
    args: dict[str, Any]
    preview_text: str
    justification: str | None
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


def _to_response(cr: ChangeRequest) -> ProvisioningRequestResponse:
    return ProvisioningRequestResponse(
        id=cr.id,
        operation=cr.operation,
        resource_type=cr.resource_type,
        resource_display=cr.resource_display,
        args=cr.args or {},
        preview_text=cr.preview_text,
        justification=cr.justification,
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


# ── Authorization helpers ──────────────────────────────────────────────────


def _require_submit(user: User) -> None:
    if is_effective_superadmin(user):
        return
    if not user_has_permission(user, "write", RESOURCE_TYPE_PROVISIONING_REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(f"Permission denied: need 'write' on '{RESOURCE_TYPE_PROVISIONING_REQUEST}'"),
        )


def _require_read(user: User) -> None:
    """Reading the portal needs ``read,provisioning_request`` — or the ability
    to decide requests, since an approver must be able to see the queue."""
    if is_effective_superadmin(user):
        return
    if user_has_permission(user, "read", RESOURCE_TYPE_PROVISIONING_REQUEST):
        return
    if user_has_permission(user, "approve", RESOURCE_TYPE_CHANGE_REQUEST):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=("Permission denied: need 'read' on " f"'{RESOURCE_TYPE_PROVISIONING_REQUEST}'"),
    )


def _can_see_all(user: User) -> bool:
    """Who sees the whole portal queue rather than only their own rows.

    Mirrors #62's read rule exactly: a request row leaks the frozen args, the
    preview, the requester and the approver, so only an effective superadmin
    or a holder of ``{approve, change_request}`` — someone who needs the queue
    to do their job — sees everyone's. A plain requester sees their own.
    """
    return is_effective_superadmin(user) or user_has_permission(
        user, "approve", RESOURCE_TYPE_CHANGE_REQUEST
    )


async def _load_visible(
    db: DB, req_id: uuid.UUID, user: User, *, for_update: bool = False
) -> ChangeRequest:
    """Fetch a portal row the caller is allowed to see, or 404.

    404 (not 403) on an invisible row so a caller cannot probe which request
    ids exist. Also 404s a gate row: the two queues are disjoint, and a portal
    endpoint must not be a side door into #62's surface.
    """
    cr = await get_change_request(db, req_id, for_update=for_update)
    if (
        cr is None
        or cr.origin != _ORIGIN
        or (not _can_see_all(user) and cr.requested_by_user_id != user.id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return cr


# ── Catalog ────────────────────────────────────────────────────────────────


@router.get("/catalog", response_model=list[RequestKindResponse])
async def get_catalog(current_user: CurrentUser) -> list[RequestKindResponse]:
    """What this caller may ask for, with the arg schema for each kind.

    Requires submit permission rather than read: the catalog exists to drive
    the request form, and a caller who cannot submit has no use for it.
    """
    _require_submit(current_user)
    out: list[RequestKindResponse] = []
    for entry in all_kinds():
        resolved = resolve_operation(entry.kind)
        if resolved is None:
            # Catalog entry naming an unregistered operation — skip it rather
            # than 500 the whole form over one bad entry.
            continue
        _, op = resolved
        out.append(
            RequestKindResponse(
                kind=entry.kind,
                label=entry.label,
                description=entry.description,
                category=entry.category,
                operation=op.name,
                args_schema=op.args_model.model_json_schema(),
            )
        )
    return out


# ── Queue ──────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ProvisioningRequestResponse])
async def list_requests(
    current_user: CurrentUser,
    db: DB,
    state: str | None = None,
    mine: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[ProvisioningRequestResponse]:
    """List self-service requests, newest first.

    A caller who cannot approve is ALWAYS scoped to their own rows regardless
    of ``mine`` — same rule as #62's queue.
    """
    _require_read(current_user)
    if _can_see_all(current_user):
        scope_user_id = current_user.id if mine else None
    else:
        scope_user_id = current_user.id
    rows = await list_change_requests(
        db,
        state=state,
        requested_by_user_id=scope_user_id,
        origin=_ORIGIN,
        limit=max(1, min(limit, 500)),
        offset=max(0, offset),
    )
    return [_to_response(r) for r in rows]


@router.post("", response_model=ProvisioningRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_request(
    body: SubmitRequestBody,
    current_user: CurrentUser,
    db: DB,
    request: Request,
) -> ProvisioningRequestResponse:
    """Submit a request. Provisions nothing — it queues a pending row that a
    *different*, suitably-permissioned approver must accept."""
    _require_submit(current_user)
    cr = await submit_request(
        db,
        user=current_user,
        request=request,
        kind=body.kind,
        args=body.args,
        justification=body.justification,
    )
    await db.commit()
    await db.refresh(cr)
    return _to_response(cr)


@router.get("/{req_id}", response_model=ProvisioningRequestResponse)
async def get_request(
    req_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ProvisioningRequestResponse:
    _require_read(current_user)
    return _to_response(await _load_visible(db, req_id, current_user))


# ── Decisions (delegated to the shared #62 spine) ──────────────────────────


@router.post("/{req_id}/approve", response_model=ProvisioningRequestResponse)
async def approve_request(
    req_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    request: Request,
    body: DecisionBody | None = None,
) -> ProvisioningRequestResponse:
    """Approve and provision.

    Every invariant is enforced by ``approve_change_request`` — the same
    function #62's router calls — so the two surfaces cannot diverge on
    authorization. Notably the approver must hold the underlying operation's
    own permission: approving a subnet request requires ``write,subnet``, so
    a request can never provision something the approver could not have
    created themselves.
    """
    # Visibility first, so an unauthorized caller gets 404 rather than a
    # permission error that confirms the id exists.
    await _load_visible(db, req_id, current_user)
    note = body.decision_note if body is not None else None
    try:
        cr = await approve_change_request(
            db, req_id, approver=current_user, request=request, note=note
        )
    except DecisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_response(cr)


@router.post("/{req_id}/reject", response_model=ProvisioningRequestResponse)
async def reject_request(
    req_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    request: Request,
    body: DecisionBody | None = None,
) -> ProvisioningRequestResponse:
    """Decline a request. Like approve, the decider may not be the requester —
    withdrawing your own request is ``cancel``."""
    await _load_visible(db, req_id, current_user)
    note = body.decision_note if body is not None else None
    try:
        cr = await reject_change_request(
            db, req_id, approver=current_user, request=request, note=note
        )
    except DecisionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _to_response(cr)


@router.post("/{req_id}/cancel", response_model=ProvisioningRequestResponse)
async def cancel_request(
    req_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    request: Request,
    body: DecisionBody | None = None,
) -> ProvisioningRequestResponse:
    """Withdraw your own pending request (superadmins may cancel any)."""
    _require_read(current_user)
    cr = await _load_visible(db, req_id, current_user, for_update=True)
    is_requester = (
        cr.requested_by_user_id is not None and current_user.id == cr.requested_by_user_id
    )
    if not is_requester and not is_effective_superadmin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the requester or a superadmin can cancel this request",
        )
    note = body.decision_note if body is not None else None
    try:
        await mark_cancelled(db, cr, user=current_user, request=request, note=note)
    except ChangeRequestStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(cr)
    return _to_response(cr)


__all__ = ["router"]
