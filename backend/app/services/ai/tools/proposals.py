"""Operator Copilot write-tool proposals (issue #90 Phase 2).

A ``propose_*`` tool is the LLM-facing entry point for a write
operation. It takes the operation's args, runs :func:`preview` to
validate + describe the change, persists an
:class:`AIOperationProposal` row, and returns a special tool-result
payload the chat surface understands as a "render an Apply / Discard
card" signal.

Crucially these tools never *apply* the mutation. Apply only runs
through ``POST /api/v1/ai/proposals/{id}/apply`` — the operator's
explicit click in the UI (or a second LLM-mediated round-trip via a
future ``apply_proposal`` tool, not yet shipping).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIOperationProposal
from app.models.auth import User
from app.services.ai import operations
from app.services.ai.operations import CreateIPAddressArgs, RunNmapScanArgs
from app.services.ai.tools.base import register_tool


# The tool result shape the frontend pattern-matches on. Stable
# contract — drawer.tsx looks for the ``kind == "proposal"`` key and
# renders an Apply / Discard card with the proposal_id wired into a
# POST /apply / POST /discard call.
def _proposal_result(proposal: AIOperationProposal, *, preview_text: str) -> dict[str, Any]:
    return {
        "kind": "proposal",
        "proposal_id": str(proposal.id),
        "operation": proposal.operation,
        "preview": preview_text,
        "expires_at": proposal.expires_at.isoformat() if proposal.expires_at else None,
        # Hint to the LLM — keep it short so the model echoes
        # something appropriate to the operator.
        "instruction": (
            "A proposal has been prepared. Tell the operator they need "
            "to review and click Apply to commit, or Discard to cancel."
        ),
    }


async def _persist_proposal(
    db: AsyncSession,
    *,
    user: User,
    operation: str,
    args: dict[str, Any],
    preview_text: str,
) -> AIOperationProposal:
    """Persist a fresh proposal row + commit. Re-used by every
    ``propose_*`` tool below.
    """
    row = AIOperationProposal(
        # session_id is set by the orchestrator (which knows the
        # active session). The tool itself doesn't have that
        # context; the orchestrator can patch it after the fact if
        # we want session-scoped listing — for Phase 2 we keep it
        # null and rely on user_id + created_at for grouping.
        session_id=None,
        user_id=user.id,
        operation=operation,
        args=args,
        preview_text=preview_text,
        expires_at=operations.expires_at_default(),
    )
    db.add(row)
    await db.flush()
    await db.commit()
    await db.refresh(row)
    return row


# ── propose_create_ip_address ─────────────────────────────────────────────


@register_tool(
    name="propose_create_ip_address",
    description=(
        "Prepare an IP-address allocation proposal. The operator must "
        "explicitly click Apply (or you must call apply_proposal with "
        "the returned proposal_id, which is not yet enabled) for the "
        "mutation to land. Returns a kind='proposal' payload — surface "
        "the preview to the operator and wait for their decision. "
        "Never call this twice for the same change without operator "
        "instruction."
    ),
    args_model=CreateIPAddressArgs,
    writes=False,  # The propose tool itself is read-only; apply is the write.
    category="ipam",
)
async def propose_create_ip_address(
    db: AsyncSession, user: User, args: CreateIPAddressArgs
) -> dict[str, Any]:
    op = operations.get_operation("create_ip_address")
    if op is None:
        return {"error": "Operation 'create_ip_address' is not registered"}
    preview = await op.preview(db, user, args)
    if not preview.ok:
        return {
            "kind": "proposal_rejected",
            "operation": "create_ip_address",
            "detail": preview.detail,
        }
    proposal = await _persist_proposal(
        db,
        user=user,
        operation="create_ip_address",
        args=args.model_dump(),
        preview_text=preview.preview_text,
    )
    return _proposal_result(proposal, preview_text=preview.preview_text)


# ── propose_run_nmap_scan ─────────────────────────────────────────────


@register_tool(
    name="propose_run_nmap_scan",
    module="tools.nmap",
    description=(
        "Prepare an nmap scan proposal. The operator must explicitly "
        "click Apply for the scan to actually run — nmap touches the "
        "network so silent execution is never appropriate. Use this "
        "for any operator question that requires *new* port / "
        "service / OS data; for *existing* scan history use "
        "list_nmap_scans + get_nmap_scan_results instead. Returns "
        "kind='proposal' — surface the preview to the operator and "
        "wait for their decision. Never call this twice for the "
        "same target without operator instruction."
    ),
    args_model=RunNmapScanArgs,
    writes=False,  # The propose tool is read-only; apply is the write.
    category="network",
)
async def propose_run_nmap_scan(
    db: AsyncSession, user: User, args: RunNmapScanArgs
) -> dict[str, Any]:
    op = operations.get_operation("run_nmap_scan")
    if op is None:
        return {"error": "Operation 'run_nmap_scan' is not registered"}
    preview = await op.preview(db, user, args)
    if not preview.ok:
        return {
            "kind": "proposal_rejected",
            "operation": "run_nmap_scan",
            "detail": preview.detail,
        }
    proposal = await _persist_proposal(
        db,
        user=user,
        operation="run_nmap_scan",
        args=args.model_dump(),
        preview_text=preview.preview_text,
    )
    return _proposal_result(proposal, preview_text=preview.preview_text)
