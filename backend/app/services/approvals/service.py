"""Change-request service — data access + audited state machine (#62).

This module owns the ``change_request`` row lifecycle: create a pending
request, read/list it, and the guarded ``mark_*`` transitions an approver
/ requester / sweep drives. Every mutation writes an ``audit_log`` row
before returning (non-negotiable #4); the caller owns the commit so the
audit row and the state change land atomically.

The gate (``gate.py``) + the approve/reject/cancel API router + the
Celery expiry sweep all call into here. Two-person authorization (approver
!= requester, approver holds ``{approve, change_request}`` AND the
operation's ``required_permission``) is enforced by the API layer *before*
calling :func:`mark_approved` / :func:`mark_executed`; this module owns
the *state-machine* invariants (a request only ever transitions out of
``pending``, and only once, under ``SELECT ... FOR UPDATE``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_meta import clean_user_agent, client_ip
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.change_request import ChangeRequest

logger = structlog.get_logger(__name__)

# Audit action strings — PLAIN verbs (#10). The event publisher maps
# resource_type ``change_request`` → namespace ``change_request`` and, for a
# verb outside the create/update/delete trio, passes the action through as
# the verb, yielding ``change_request.<verb>`` (e.g. ``change_request.requested``).
# Using already-namespaced strings here would double the namespace
# (``change_request.change_request.requested``) — so keep these bare verbs.
_ACTION_REQUESTED = "requested"
_ACTION_APPROVED = "approved"
_ACTION_REJECTED = "rejected"
_ACTION_CANCELLED = "cancelled"
_ACTION_EXECUTED = "executed"
_ACTION_FAILED = "failed"
_ACTION_EXPIRED = "expired"


class ChangeRequestStateError(Exception):
    """A transition was attempted from an illegal current state.

    The API layer translates this into a 409 Conflict — e.g. two
    approvers race and the second one finds the row already
    ``approved``/``executed``.
    """


def _audit(
    db: AsyncSession,
    *,
    user: User | None,
    request: Request | None,
    action: str,
    cr: ChangeRequest,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Append an ``audit_log`` row for a change-request transition.

    ``user`` is the actor (None only for the system-driven expiry sweep,
    which records a synthetic ``system`` actor). ``request`` is None on
    non-HTTP paths (the Celery sweep) — source_ip / user_agent stay NULL.
    """
    db.add(
        AuditLog(
            user_id=user.id if user is not None else None,
            user_display_name=user.display_name if user is not None else "system",
            auth_source=user.auth_source if user is not None else "system",
            source_ip=client_ip(request) if request is not None else None,
            user_agent=(
                clean_user_agent(request.headers.get("user-agent")) if request is not None else None
            ),
            action=action,
            resource_type="change_request",
            resource_id=str(cr.id),
            resource_display=cr.resource_display,
            old_value=old_value,
            new_value=new_value,
        )
    )


# ── Create / read ────────────────────────────────────────────────────────


async def create_change_request(
    db: AsyncSession,
    *,
    user: User,
    request: Request,
    operation: str,
    resource_type: str,
    resource_id: str | None,
    resource_display: str,
    args: dict[str, Any],
    preview_text: str,
    risk_reason: str,
    ttl_hours: int,
) -> ChangeRequest:
    """Persist a new ``pending`` change request + its ``requested`` audit row.

    The caller (the gate) owns the commit. ``ttl_hours`` comes from the
    matched policy; ``expires_at`` is stamped now + ttl.
    """
    now = datetime.now(UTC)
    cr = ChangeRequest(
        operation=operation,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_display=resource_display,
        args=args,
        preview_text=preview_text,
        risk_reason=risk_reason,
        state="pending",
        requested_by_user_id=user.id,
        requested_by_display=user.display_name,
        expires_at=now + timedelta(hours=ttl_hours),
    )
    db.add(cr)
    # Flush so the row gets its id before the audit row references it.
    await db.flush()
    _audit(
        db,
        user=user,
        request=request,
        action=_ACTION_REQUESTED,
        cr=cr,
        new_value={
            "operation": operation,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "risk_reason": risk_reason,
            "state": "pending",
        },
    )
    logger.info(
        "change_request.created",
        change_request_id=str(cr.id),
        operation=operation,
        resource_type=resource_type,
        requested_by=str(user.id),
    )
    return cr


async def get_change_request(
    db: AsyncSession, cr_id: UUID, *, for_update: bool = False
) -> ChangeRequest | None:
    """Load one change request. ``for_update`` takes a row-level lock so a
    transition is race-safe against a concurrent approver (#62 concurrency
    edge-case)."""
    stmt = select(ChangeRequest).where(ChangeRequest.id == cr_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_change_requests(
    db: AsyncSession,
    *,
    state: str | None = None,
    resource_type: str | None = None,
    requested_by_user_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ChangeRequest]:
    """List change requests newest-first with optional filters."""
    stmt = select(ChangeRequest)
    if state is not None:
        stmt = stmt.where(ChangeRequest.state == state)
    if resource_type is not None:
        stmt = stmt.where(ChangeRequest.resource_type == resource_type)
    if requested_by_user_id is not None:
        stmt = stmt.where(ChangeRequest.requested_by_user_id == requested_by_user_id)
    stmt = stmt.order_by(ChangeRequest.created_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(stmt)).scalars().all())


# ── State transitions (each guarded + audited) ─────────────────────────────


def _require_state(cr: ChangeRequest, expected: str) -> None:
    if cr.state != expected:
        raise ChangeRequestStateError(
            f"change request {cr.id} is {cr.state!r}, expected {expected!r}"
        )


async def mark_approved(
    db: AsyncSession,
    cr: ChangeRequest,
    *,
    approver: User,
    request: Request | None,
    note: str | None,
) -> ChangeRequest:
    """``pending`` → ``approved``. Caller must have already loaded ``cr``
    with ``for_update=True`` and enforced two-person RBAC. Records the
    approver + decision time; execution (``apply()``) is a separate
    :func:`mark_executed` / :func:`mark_failed` call by the API layer."""
    _require_state(cr, "pending")
    cr.state = "approved"
    cr.decided_by_user_id = approver.id
    cr.decided_by_display = approver.display_name
    cr.decision_note = note
    cr.decided_at = datetime.now(UTC)
    _audit(
        db,
        user=approver,
        request=request,
        action=_ACTION_APPROVED,
        cr=cr,
        old_value={"state": "pending"},
        new_value={"state": "approved", "note": note},
    )
    return cr


async def mark_rejected(
    db: AsyncSession,
    cr: ChangeRequest,
    *,
    approver: User,
    request: Request | None,
    note: str | None,
) -> ChangeRequest:
    """``pending`` → ``rejected`` (terminal)."""
    _require_state(cr, "pending")
    cr.state = "rejected"
    cr.decided_by_user_id = approver.id
    cr.decided_by_display = approver.display_name
    cr.decision_note = note
    cr.decided_at = datetime.now(UTC)
    _audit(
        db,
        user=approver,
        request=request,
        action=_ACTION_REJECTED,
        cr=cr,
        old_value={"state": "pending"},
        new_value={"state": "rejected", "note": note},
    )
    return cr


async def mark_cancelled(
    db: AsyncSession,
    cr: ChangeRequest,
    *,
    user: User,
    request: Request | None,
) -> ChangeRequest:
    """``pending`` → ``cancelled`` (terminal). Driven by the requester (or
    a superadmin); the API layer enforces who may cancel."""
    _require_state(cr, "pending")
    cr.state = "cancelled"
    cr.decided_by_user_id = user.id
    cr.decided_by_display = user.display_name
    cr.decided_at = datetime.now(UTC)
    _audit(
        db,
        user=user,
        request=request,
        action=_ACTION_CANCELLED,
        cr=cr,
        old_value={"state": "pending"},
        new_value={"state": "cancelled"},
    )
    return cr


async def mark_executed(
    db: AsyncSession,
    cr: ChangeRequest,
    *,
    approver: User,
    requester_id: UUID | None,
    request: Request | None,
    result: dict[str, Any],
) -> ChangeRequest:
    """``approved`` → ``executed`` (terminal). The operation's ``apply()``
    ran successfully under the approver's identity. The audit row sets
    ``user_id=approver`` and ``old_value.requested_by=<requester>`` so the
    two-person trail is queryable (#62)."""
    _require_state(cr, "approved")
    cr.state = "executed"
    cr.result = result
    cr.executed_at = datetime.now(UTC)
    _audit(
        db,
        user=approver,
        request=request,
        action=_ACTION_EXECUTED,
        cr=cr,
        old_value={
            "state": "approved",
            "requested_by": str(requester_id) if requester_id is not None else None,
        },
        new_value={"state": "executed", "result": result},
    )
    logger.info(
        "change_request.executed",
        change_request_id=str(cr.id),
        operation=cr.operation,
        approved_by=str(approver.id),
        requested_by=str(requester_id) if requester_id is not None else None,
    )
    return cr


async def mark_failed(
    db: AsyncSession,
    cr: ChangeRequest,
    *,
    approver: User,
    requester_id: UUID | None,
    request: Request | None,
    error: str,
) -> ChangeRequest:
    """``approved`` → ``failed`` (terminal). ``apply()`` (or its
    re-preview stale-state guard) failed at execution time. Like
    :func:`mark_executed`, the audit row carries the requester→approver
    correlation (#14): ``user_id=approver`` + ``old_value.requested_by``."""
    _require_state(cr, "approved")
    cr.state = "failed"
    cr.error = error
    cr.executed_at = datetime.now(UTC)
    _audit(
        db,
        user=approver,
        request=request,
        action=_ACTION_FAILED,
        cr=cr,
        old_value={
            "state": "approved",
            "requested_by": str(requester_id) if requester_id is not None else None,
        },
        new_value={"state": "failed", "error": error},
    )
    logger.warning(
        "change_request.failed",
        change_request_id=str(cr.id),
        operation=cr.operation,
        error=error,
    )
    return cr


async def mark_expired(db: AsyncSession, cr: ChangeRequest) -> ChangeRequest:
    """``pending`` → ``expired`` (terminal). System-driven by the Celery
    sweep; no HTTP request / human actor."""
    _require_state(cr, "pending")
    cr.state = "expired"
    cr.decided_at = datetime.now(UTC)
    _audit(
        db,
        user=None,
        request=None,
        action=_ACTION_EXPIRED,
        cr=cr,
        old_value={"state": "pending"},
        new_value={"state": "expired"},
    )
    logger.info("change_request.expired", change_request_id=str(cr.id))
    return cr


# ── Shared approve / reject orchestration (the two-person spine) ───────────
#
# These functions own the full two-person decision flow so the HTTP router
# (api/v1/change_requests/router.py) and the AI propose→apply path
# (services/ai/operations.py: approve_change_request / reject_change_request)
# enforce the *identical* invariants without copy-pasting them. The
# invariants are enforced here, never trusting the caller:
#
#   1. Row loaded ``FOR UPDATE`` + must still be ``pending`` (racing
#      approver loses → ``DecisionConflict``); an expired row flips to
#      ``expired`` and raises ``DecisionConflict``.
#   2. Self-approval blocked — approver != requester (``DecisionForbidden``).
#   3. Approver holds ``{approve, change_request}`` (``DecisionForbidden``).
#   4. Approver holds the underlying op's ``required_permission``
#      (``DecisionForbidden``) — can't rubber-stamp a delete you couldn't do.
#   5. ``preview()`` re-runs (stale-state guard); if not ok the op does NOT
#      execute (``DecisionConflict``), the row stays ``pending``.
#   6. ``apply()`` dispatches under the **approver's** identity; the audit
#      ``executed`` row carries approver as actor + requester in
#      ``old_value.requested_by``.
#
# The error subclasses carry an HTTP-ish ``status_code`` so the two call
# sites translate them uniformly. ``request`` is optional — None on the
# AI propose→apply path (no FastAPI Request there); audit source_ip /
# user_agent stay NULL exactly like the Celery sweep.


class DecisionError(Exception):
    """Base for a refused approve/reject decision. ``status_code`` lets the
    caller map it to the right HTTP response."""

    status_code: int = 400


class DecisionNotFound(DecisionError):
    status_code = 404


class DecisionForbidden(DecisionError):
    status_code = 403


class DecisionConflict(DecisionError):
    status_code = 409


class DecisionUnprocessable(DecisionError):
    status_code = 422


def _decision_for_status(status_code: int, detail: str) -> DecisionError:
    """Map an HTTP 4xx ``apply()`` raised to the matching DecisionError so the
    approve callers surface the precondition status (#11)."""
    mapping: dict[int, type[DecisionError]] = {
        403: DecisionForbidden,
        404: DecisionNotFound,
        409: DecisionConflict,
        422: DecisionUnprocessable,
    }
    return mapping.get(status_code, DecisionConflict)(detail)


async def _safe_rollback(db: AsyncSession) -> None:
    """Roll back, swallowing a rollback-time failure (#15).

    If ``apply()`` already rolled back (or the connection is wedged) a second
    rollback can itself raise; letting that propagate would mask the original
    error and could strand the row. Best-effort + log, never re-raise.
    """
    try:
        await db.rollback()
    except Exception as exc:  # noqa: BLE001
        logger.warning("change_request.rollback_failed", error=str(exc))


async def _stamp_apply_failed(
    db: AsyncSession,
    cr_id: UUID,
    *,
    approver: User,
    request: Request | None,
    note: str | None,
    error: str,
) -> ChangeRequest:
    """Re-lock the (rolled-back) row and stamp ``pending → approved → failed``.

    Shared by the approve spine's 5xx and generic-exception paths. The
    approval transition was rolled back alongside ``apply()``, so replay it
    before ``mark_failed``. Raises :class:`DecisionError` if the row vanished
    or another actor flipped it mid-apply."""
    cr = await get_change_request(db, cr_id, for_update=True)
    if cr is None or cr.state != "pending":
        raise DecisionError("Change request vanished or changed state mid-apply — investigate")
    requester_id = cr.requested_by_user_id
    await mark_approved(db, cr, approver=approver, request=request, note=note)
    await mark_failed(
        db, cr, approver=approver, requester_id=requester_id, request=request, error=error
    )
    await db.commit()
    logger.warning(
        "change_request.apply_failed",
        change_request_id=str(cr.id),
        operation=cr.operation,
        error=error,
    )
    return cr


async def approve_change_request(
    db: AsyncSession,
    cr_id: UUID,
    *,
    approver: User,
    request: Request | None,
    note: str | None,
) -> ChangeRequest:
    """Approve + execute a pending change request — the two-person spine.

    Loads the row ``FOR UPDATE``, enforces every server-side invariant
    (steps 1–6 above), runs the underlying operation's ``apply()`` under
    ``approver``'s identity, and **commits**. On a successful apply the row
    is ``executed`` (with ``result``); on an apply-time failure the row is
    ``failed`` (with ``error``) and the function returns normally (the
    caller surfaces the terminal row). Raises a :class:`DecisionError`
    subclass for every refused-decision case before any mutation lands.
    """
    # Local import — operations imports this module's siblings, so importing
    # it at module top would risk a cycle.
    from app.services.ai import operations  # noqa: PLC0415
    from app.services.ai.operations import OperationPermissionError  # noqa: PLC0415

    # 1. Row lock + state guard — race-safe against a second approver.
    cr = await get_change_request(db, cr_id, for_update=True)
    if cr is None:
        raise DecisionNotFound(f"Change request {cr_id} not found")
    if cr.state != "pending":
        raise DecisionConflict(f"Change request is not pending (state={cr.state!r})")
    if cr.expires_at < datetime.now(UTC):
        # #13: route through the audited mark_expired transition (not a raw
        # field poke) so the change_request.expired audit row lands.
        await mark_expired(db, cr)
        await db.commit()
        raise DecisionConflict("Change request has expired")

    # 2a. Fail CLOSED when the requester row is gone (#5). ON DELETE SET NULL
    #     nulls ``requested_by_user_id`` if the requester is deleted; the
    #     self-approval guard below would then fail OPEN (None != approver.id
    #     is always true), letting anyone rubber-stamp an orphaned request and
    #     defeating two-person. Refuse instead — the requester must recreate.
    if cr.requested_by_user_id is None:
        raise DecisionConflict(
            "Requester no longer exists; cancel and recreate this change request"
        )

    # 2b. Self-approval is forbidden — the whole point of two-person.
    if approver.id == cr.requested_by_user_id:
        raise DecisionForbidden("You cannot approve your own change request")

    op = operations.get_operation(cr.operation)
    if op is None:
        raise DecisionError(f"Operation {cr.operation!r} is not registered")

    # 3. Approver holds the approve capability.
    if not _can_approve(approver):
        raise DecisionForbidden(
            "Permission denied: need 'approve' on 'change_request'",
        )

    # 4. Approver holds the underlying operation's own permission.
    try:
        operations.enforce_operation_permission(approver, op)
    except OperationPermissionError as exc:
        raise DecisionForbidden(str(exc)) from exc

    # Re-validate frozen args (guards the rare redeploy-mid-flight schema change).
    try:
        args = op.args_model.model_validate(cr.args or {})
    except Exception as exc:  # noqa: BLE001
        raise DecisionUnprocessable(f"Stored args no longer validate: {exc}") from exc

    # 5. Stale-state re-check — do NOT execute a doomed op; leave it pending.
    preview = await op.preview(db, approver, args)
    if not preview.ok:
        logger.info(
            "change_request.stale",
            change_request_id=str(cr.id),
            operation=cr.operation,
            detail=preview.detail,
            approver=str(approver.id),
        )
        raise DecisionConflict(preview.detail)

    requester_id = cr.requested_by_user_id

    # Record the approval transition (pending → approved, audited).
    await mark_approved(db, cr, approver=approver, request=request, note=note)

    # 6. Dispatch apply() under the approver's identity. apply() runs its own
    #    mutation + audit + commit; on success mark_executed writes the
    #    change_request.executed audit row carrying both ids.
    try:
        result = await op.apply(db, approver, args)
    except OperationPermissionError as exc:
        await _safe_rollback(db)
        raise DecisionForbidden(str(exc)) from exc
    except HTTPException as exc:
        # #11: a client 4xx from apply() is NOT an execution failure — it's a
        # raced precondition the op rejected (e.g. the subnet became
        # non-empty, or the row vanished, between preview and apply). Surface
        # the same status, leave the request PENDING (the requester can cancel
        # or retry once the precondition clears). Only a 5xx (server fault)
        # falls through to mark_failed below.
        await _safe_rollback(db)
        sc = exc.status_code
        if 400 <= sc < 500:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            logger.info(
                "change_request.apply_precondition",
                change_request_id=str(cr_id),
                operation=op.name,
                status_code=sc,
                detail=detail,
            )
            raise _decision_for_status(sc, detail) from exc
        return await _stamp_apply_failed(
            db, cr_id, approver=approver, request=request, note=note, error=str(exc.detail)
        )
    except Exception as exc:  # noqa: BLE001
        # apply() rolled back its own transaction; reload + lock to stamp the
        # failure (the approval transition was rolled back too).
        await _safe_rollback(db)
        return await _stamp_apply_failed(
            db, cr_id, approver=approver, request=request, note=note, error=str(exc)
        )

    await mark_executed(
        db,
        cr,
        approver=approver,
        requester_id=requester_id,
        request=request,
        result=result,
    )
    await db.commit()
    await db.refresh(cr)
    return cr


async def reject_change_request(
    db: AsyncSession,
    cr_id: UUID,
    *,
    approver: User,
    request: Request | None,
    note: str | None,
) -> ChangeRequest:
    """Decline a pending change request (``pending`` → ``rejected``).

    Needs ``approve,change_request``; like approve, the rejecter may not be
    the requester (a self-reject is a cancel). Commits + returns the terminal
    row, or raises a :class:`DecisionError` subclass for a refused decision.
    """
    if not _can_approve(approver):
        raise DecisionForbidden("Permission denied: need 'approve' on 'change_request'")
    cr = await get_change_request(db, cr_id, for_update=True)
    if cr is None:
        raise DecisionNotFound(f"Change request {cr_id} not found")
    if cr.requested_by_user_id is not None and approver.id == cr.requested_by_user_id:
        raise DecisionForbidden("You cannot reject your own request — cancel it instead")
    try:
        await mark_rejected(db, cr, approver=approver, request=request, note=note)
    except ChangeRequestStateError as exc:
        raise DecisionConflict(str(exc)) from exc
    await db.commit()
    await db.refresh(cr)
    return cr


def _can_approve(user: User) -> bool:
    """True when ``user`` holds ``{approve, change_request}``. Local import
    of the permission helper avoids an import cycle at module load."""
    from app.core.permissions import (  # noqa: PLC0415
        RESOURCE_TYPE_CHANGE_REQUEST,
        user_has_permission,
    )

    return user_has_permission(user, "approve", RESOURCE_TYPE_CHANGE_REQUEST)


__all__ = [
    "ChangeRequestStateError",
    "DecisionConflict",
    "DecisionError",
    "DecisionForbidden",
    "DecisionNotFound",
    "DecisionUnprocessable",
    "approve_change_request",
    "create_change_request",
    "get_change_request",
    "list_change_requests",
    "mark_approved",
    "mark_cancelled",
    "mark_executed",
    "mark_expired",
    "mark_failed",
    "mark_rejected",
    "reject_change_request",
]
