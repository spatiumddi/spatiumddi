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
from app.services.ai.operations import (
    ArchiveSessionArgs,
    CreateAlertRuleArgs,
    CreateDHCPStaticArgs,
    CreateDNSRecordArgs,
    CreateIPAddressArgs,
    RunNmapScanArgs,
)
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


# ── Tier 5 propose_* tools (issue #101) ───────────────────────────────
#
# Each one mirrors the existing pattern: tool itself is read-only
# (writes=False — the actual mutation happens at /apply time after
# operator approval); the underlying registered Operation enforces
# the preview + apply contract; ``_persist_proposal`` writes the
# AIOperationProposal row that the chat drawer renders as an Approve
# / Reject card.
#
# All four ship default-disabled so an operator who hasn't reviewed
# the implications doesn't accidentally hand the LLM keys to their
# DNS / DHCP / alert / chat tables. Enable per-tool via Settings →
# AI → Tool Catalog (the catalog page now confirm-modals before
# turning on any propose_* tool — see frontend treatment of the
# ``propose_`` name prefix).


async def _propose_via(
    *,
    db: AsyncSession,
    user: User,
    operation_name: str,
    args: Any,
) -> dict[str, Any]:
    """Shared boilerplate — look up the Operation, run preview, persist
    proposal on success, surface the rejection on failure."""
    op = operations.get_operation(operation_name)
    if op is None:
        return {"error": f"Operation {operation_name!r} is not registered"}
    preview = await op.preview(db, user, args)
    if not preview.ok:
        return {
            "kind": "proposal_rejected",
            "operation": operation_name,
            "detail": preview.detail,
        }
    proposal = await _persist_proposal(
        db,
        user=user,
        operation=operation_name,
        args=args.model_dump(),
        preview_text=preview.preview_text,
    )
    return _proposal_result(proposal, preview_text=preview.preview_text)


# ── propose_create_dns_record ─────────────────────────────────────────


@register_tool(
    name="propose_create_dns_record",
    description=(
        "Prepare a DNS record creation proposal. Operator must click "
        "Approve in the chat drawer to apply — DNS edits propagate to "
        "live BIND9 / Windows DNS servers. Use when the operator says "
        "'create an A record for foo pointing at 10.0.0.5' or similar. "
        "Pass zone_id (UUID), name (relative — '@' for apex), "
        "record_type, value; ttl + priority are optional. Returns a "
        "kind='proposal' card; never call twice for the same change."
    ),
    args_model=CreateDNSRecordArgs,
    writes=False,
    category="dns",
    default_enabled=False,
)
async def propose_create_dns_record(
    db: AsyncSession, user: User, args: CreateDNSRecordArgs
) -> dict[str, Any]:
    return await _propose_via(db=db, user=user, operation_name="create_dns_record", args=args)


# ── propose_create_dhcp_static ────────────────────────────────────────


@register_tool(
    name="propose_create_dhcp_static",
    description=(
        "Prepare a DHCP static reservation proposal. Operator must "
        "click Approve to apply — the reservation propagates to the "
        "Kea / Windows DHCP backend. Pass scope_id (UUID), ip_address "
        "(must lie inside the scope), mac_address; hostname + "
        "description are optional. Use when the operator says 'pin "
        "11:22:33:44:55:66 to 10.0.0.7 in the corp scope'."
    ),
    args_model=CreateDHCPStaticArgs,
    writes=False,
    category="dhcp",
    default_enabled=False,
)
async def propose_create_dhcp_static(
    db: AsyncSession, user: User, args: CreateDHCPStaticArgs
) -> dict[str, Any]:
    return await _propose_via(db=db, user=user, operation_name="create_dhcp_static", args=args)


# ── propose_create_alert_rule ─────────────────────────────────────────


@register_tool(
    name="propose_create_alert_rule",
    description=(
        "Prepare a subnet-utilization alert rule proposal. Pass name, "
        "threshold_percent (1-100), severity (info / warning / "
        "critical), and an optional description. Other rule_type "
        "values keep their UI authoring path; this proposer is "
        "scoped to subnet-utilization which is the most common "
        "operator request. Returns a kind='proposal' card; operator "
        "clicks Approve to actually create the rule."
    ),
    args_model=CreateAlertRuleArgs,
    writes=False,
    category="ops",
    default_enabled=False,
)
async def propose_create_alert_rule(
    db: AsyncSession, user: User, args: CreateAlertRuleArgs
) -> dict[str, Any]:
    return await _propose_via(db=db, user=user, operation_name="create_alert_rule", args=args)


# ── propose_archive_session ───────────────────────────────────────────


@register_tool(
    name="propose_archive_session",
    description=(
        "Prepare a chat-session archive proposal. Hides the named "
        "session from the History panel's default view without "
        "deleting it; the row stays restorable. Operator can only "
        "archive their own sessions — preview rejects cross-user "
        "attempts. Use when the operator says 'archive this chat' or "
        "'hide my old debugging sessions'."
    ),
    args_model=ArchiveSessionArgs,
    writes=False,
    category="ops",
    default_enabled=False,
)
async def propose_archive_session(
    db: AsyncSession, user: User, args: ArchiveSessionArgs
) -> dict[str, Any]:
    return await _propose_via(db=db, user=user, operation_name="archive_session", args=args)
