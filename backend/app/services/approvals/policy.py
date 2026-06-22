"""Approval policy engine (#62).

Pure async decision layer: given an ``(resource_type, action[, count])``
tuple, return the strongest *enabled* :class:`ApprovalPolicy` that
matches, or ``None`` when no approval is required. Kept side-effect-free
and superadmin-agnostic on purpose — the *gate* (``gate.py``, later
slice) decides the superadmin bypass using the returned row's
``applies_to_superadmin`` flag. Splitting the decision this way keeps the
engine trivially testable and the gate the single place authorization is
applied.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_request import ApprovalPolicy

logger = structlog.get_logger(__name__)


async def match_policy(
    db: AsyncSession,
    resource_type: str,
    action: str,
    count: int | None = None,
) -> ApprovalPolicy | None:
    """Strongest enabled policy matching ``(resource_type | "*") + action``.

    A row matches when it is ``enabled``, its ``action`` equals ``action``,
    and its ``resource_type`` is either ``resource_type`` or the wildcard
    ``"*"``. A matched row's threshold is satisfied when ``min_count IS
    NULL`` (always require approval) OR ``count`` is provided and
    ``count >= min_count``.

    An exact ``resource_type`` match wins over a ``"*"`` match. Among rows
    of equal specificity, the one with the *lower* ``min_count`` (the
    stricter threshold; ``NULL`` is strictest) wins — so the request is
    gated by the tightest applicable rule.

    Returns ``None`` when nothing matches → no approval required, the
    caller proceeds inline. The superadmin bypass is decided by the CALLER
    via the returned row's ``applies_to_superadmin`` flag, NOT here.
    """
    rows = (
        await db.execute(
            select(ApprovalPolicy).where(
                ApprovalPolicy.enabled.is_(True),
                ApprovalPolicy.action == action,
                ApprovalPolicy.resource_type.in_([resource_type, "*"]),
            )
        )
    ).scalars()

    best: ApprovalPolicy | None = None
    for policy in rows:
        # Threshold gate.
        if policy.min_count is not None and (count is None or count < policy.min_count):
            continue
        if best is None or _is_stronger(policy, best, resource_type):
            best = policy

    if best is not None:
        logger.debug(
            "approval_policy.matched",
            resource_type=resource_type,
            action=action,
            count=count,
            policy_id=str(best.id),
            policy_name=best.name,
        )
    return best


def _is_stronger(candidate: ApprovalPolicy, current: ApprovalPolicy, resource_type: str) -> bool:
    """True if ``candidate`` should beat the currently-selected ``current``.

    Exact ``resource_type`` beats wildcard ``"*"``. At equal specificity,
    the stricter threshold wins — ``min_count IS NULL`` (always) beats any
    numeric threshold, and a lower numeric threshold beats a higher one.
    """
    cand_exact = candidate.resource_type == resource_type
    curr_exact = current.resource_type == resource_type
    if cand_exact != curr_exact:
        return cand_exact

    # Equal specificity → compare thresholds (None == strictest).
    if candidate.min_count is None:
        return current.min_count is not None
    if current.min_count is None:
        return False
    return candidate.min_count < current.min_count


__all__ = ["match_policy"]
