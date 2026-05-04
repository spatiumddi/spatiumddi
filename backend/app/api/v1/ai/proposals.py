"""Operator Copilot operation-proposal API (issue #90 Phase 2).

Endpoints to list, get, apply, and discard pending write-operation
proposals. The chat surface fires ``POST /apply`` when the operator
clicks the Apply button on a proposal card (the propose tool ran
earlier and persisted the proposal); ``POST /discard`` is the
explicit reject button.

Permissions:
    * **List / get / apply / discard** are all scoped to the calling
      user — a row created by user A cannot be applied or even
      observed by user B. Superadmin gets the same scoping; if cross-
      user proposal management is needed it'll come in a follow-up.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.models.ai import AIOperationProposal
from app.services.ai import operations

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────


class ProposalResponse(BaseModel):
    id: uuid.UUID
    operation: str
    args: dict[str, Any]
    preview_text: str
    expires_at: datetime
    applied_at: datetime | None
    discarded_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime


class ApplyResponse(BaseModel):
    ok: bool
    detail: str
    result: dict[str, Any] | None
    proposal: ProposalResponse


# ── Helpers ──────────────────────────────────────────────────────────────


def _to_response(p: AIOperationProposal) -> ProposalResponse:
    return ProposalResponse(
        id=p.id,
        operation=p.operation,
        args=p.args or {},
        preview_text=p.preview_text or "",
        expires_at=p.expires_at,
        applied_at=p.applied_at,
        discarded_at=p.discarded_at,
        result=p.result,
        error=p.error,
        created_at=p.created_at,
    )


def _terminal_state(p: AIOperationProposal) -> str | None:
    """Return a human-readable terminal state, or None if still pending."""
    if p.applied_at is not None:
        return f"already applied at {p.applied_at.isoformat()}"
    if p.discarded_at is not None:
        return f"already discarded at {p.discarded_at.isoformat()}"
    if p.expires_at < datetime.now(UTC):
        return f"expired at {p.expires_at.isoformat()}"
    return None


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/proposals", response_model=list[ProposalResponse])
async def list_proposals(
    current_user: CurrentUser,
    db: DB,
    pending_only: bool = True,
) -> list[ProposalResponse]:
    stmt = select(AIOperationProposal).where(AIOperationProposal.user_id == current_user.id)
    if pending_only:
        stmt = stmt.where(
            AIOperationProposal.applied_at.is_(None),
            AIOperationProposal.discarded_at.is_(None),
            AIOperationProposal.expires_at > datetime.now(UTC),
        )
    stmt = stmt.order_by(AIOperationProposal.created_at.desc()).limit(50)
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(r) for r in rows]


@router.get("/proposals/{proposal_id}", response_model=ProposalResponse)
async def get_proposal(
    proposal_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ProposalResponse:
    row = await db.get(AIOperationProposal, proposal_id)
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_response(row)


@router.post("/proposals/{proposal_id}/apply", response_model=ApplyResponse)
async def apply_proposal(
    proposal_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ApplyResponse:
    row = await db.get(AIOperationProposal, proposal_id)
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    terminal = _terminal_state(row)
    if terminal is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot apply — proposal {terminal}",
        )
    op = operations.get_operation(row.operation)
    if op is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Operation {row.operation!r} is not registered",
        )

    # Re-validate args through the operation's pydantic model — guards
    # against the rare case where the model schema changed between
    # propose and apply (a redeploy mid-flight).
    try:
        args = op.args_model.model_validate(row.args or {})
    except Exception as exc:
        # Surface the validation failure as the proposal's error
        # state so the UI shows the right message.
        row.error = f"args validation failed: {exc}"
        row.discarded_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
        return ApplyResponse(
            ok=False,
            detail=row.error,
            result=None,
            proposal=_to_response(row),
        )

    try:
        result = await op.apply(db, current_user, args)
    except Exception as exc:  # noqa: BLE001
        # Apply rolled back its own transaction; reload the proposal
        # from a fresh state and stamp the error.
        await db.rollback()
        row = await db.get(AIOperationProposal, proposal_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Proposal vanished mid-apply — investigate",
            ) from exc
        row.error = str(exc)
        row.applied_at = None
        await db.commit()
        await db.refresh(row)
        logger.warning(
            "ai_proposal_apply_failed",
            proposal_id=str(proposal_id),
            operation=row.operation,
            error=str(exc),
        )
        return ApplyResponse(
            ok=False,
            detail=str(exc),
            result=None,
            proposal=_to_response(row),
        )

    # The operation's own apply() already committed the mutation to
    # whatever rows it touches. We follow up with a separate write to
    # stamp the proposal's terminal state — the audit row from the
    # apply path is independent so a partial-stamp doesn't unwind the
    # mutation.
    row = await db.get(AIOperationProposal, proposal_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Proposal vanished mid-apply",
        )
    row.applied_at = datetime.now(UTC)
    row.result = result if isinstance(result, dict) else {"value": result}
    row.error = None
    await db.commit()
    await db.refresh(row)

    logger.info(
        "ai_proposal_applied",
        proposal_id=str(proposal_id),
        operation=row.operation,
        user_id=str(current_user.id),
    )
    return ApplyResponse(
        ok=True,
        detail="applied",
        result=row.result,
        proposal=_to_response(row),
    )


@router.post("/proposals/{proposal_id}/discard", response_model=ProposalResponse)
async def discard_proposal(
    proposal_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ProposalResponse:
    row = await db.get(AIOperationProposal, proposal_id)
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row.applied_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot discard — already applied",
        )
    if row.discarded_at is None:
        row.discarded_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
    return _to_response(row)
