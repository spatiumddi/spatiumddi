"""Approval gate — the single interception point risky handlers call (#62).

A covered delete handler calls :func:`gate_or_execute` at the top. The
return value tells the handler what to do:

* ``None``  → no approval required. The handler executes inline exactly as
  today (delegating to ``operation.apply``). This is the *default* path:
  the ``governance.approvals`` module is off out of the box, and even when
  on, only enabled policies gate. Behaviour is byte-identical to pre-#62.
* :class:`ChangeRequestPending` → a policy matched and the caller can't
  self-approve. A ``change_request`` row is persisted (+ its ``requested``
  audit row, committed) and the handler must return ``202 Accepted`` with
  ``pending.as_dict()`` instead of executing.

Control flow (pinned by the #62 design):

1. Module off → ``None`` (never touches the policy table).
2. Derive ``(action, resource_type)`` for the policy lookup from the
   operation (delete-family → ``action="delete"``; resource_type is the
   operation's ``required_permission[1]``).
3. ``match_policy`` → no enabled match → ``None``.
4. Superadmin caller + ``not policy.applies_to_superadmin`` → ``None``.
5. ``operation.preview`` → if not ok, raise 409 (don't queue a doomed
   request — matches today's inline 409).
6. Persist the ``change_request`` (+ audit), commit, return the pending.

#4 (audit-before-respond): the ``requested`` audit row is committed inside
:func:`create_change_request` before this returns. #2 (async throughout):
everything here is async.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_effective_superadmin
from app.models.auth import User
from app.services.ai.operations import Operation
from app.services.approvals.policy import match_policy
from app.services.approvals.service import create_change_request
from app.services.feature_modules import is_module_enabled

logger = structlog.get_logger(__name__)

MODULE_ID = "governance.approvals"

# Maps a registered risky operation name to the (action, ...) the policy
# lookup keys on. The resource_type comes from the operation's
# ``required_permission[1]`` so the policy ``resource_type`` matches the
# seeded rows (``subnet`` / ``ip_block`` / ``ip_space`` / ``dns_zone`` /
# ``dhcp_scope`` / ``dhcp_server_group``). Every covered op is a delete.
OPERATION_ACTION: dict[str, str] = {
    "delete_subnet": "delete",
    "delete_block": "delete",
    "delete_space": "delete",
    "delete_zone": "delete",
    "delete_scope": "delete",
    "delete_group": "delete",
}


@dataclass(frozen=True)
class ChangeRequestPending:
    """Returned when an operation was queued for approval instead of run."""

    change_request_id: Any
    state: str
    preview_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "change_request_id": str(self.change_request_id),
            "state": self.state,
            "preview_text": self.preview_text,
        }


def _resource_id_from_args(args: BaseModel) -> str | None:
    """Best-effort single-target id for the change_request row.

    Covered ops carry a single ``*_id`` (subnet_id / block_id / space_id /
    zone_id / scope_id / group_id). Pick the first such field; ops with no
    single target leave ``resource_id`` NULL.
    """
    data = args.model_dump(mode="json")
    # Prefer the operation's own target id over a secondary (e.g. delete_zone
    # carries both group_id and zone_id — zone_id is the real target).
    for key in ("zone_id", "scope_id", "subnet_id", "block_id", "space_id", "group_id"):
        val = data.get(key)
        if val is not None:
            return str(val)
    return None


async def gate_or_execute(
    db: AsyncSession,
    user: User,
    request: Request,
    *,
    operation: Operation,
    args: BaseModel,
    count: int | None = None,
) -> ChangeRequestPending | None:
    """Decide whether ``operation`` must be queued for second-person approval.

    Returns ``None`` to tell the caller to execute inline (today's behaviour);
    a :class:`ChangeRequestPending` to tell the caller to return 202.
    """
    # 1. Module off → never gate, never query policies. This is the path
    #    every existing install takes (default-off) → zero behaviour change.
    if not await is_module_enabled(db, MODULE_ID):
        return None

    # 2. Derive the policy lookup keys from the operation.
    action = OPERATION_ACTION.get(operation.name)
    if action is None or operation.required_permission is None:
        # An operation not in the action map isn't gateable — run inline.
        return None
    resource_type = operation.required_permission[1]

    # 3. Strongest enabled matching policy, or None.
    policy = await match_policy(db, resource_type, action, count)
    if policy is None:
        return None

    # 4. Superadmin bypass — only when the policy explicitly allows it.
    if is_effective_superadmin(user) and not policy.applies_to_superadmin:
        return None

    # 5. Re-run preview now; don't queue a request that can't execute.
    preview = await operation.preview(db, user, args)
    if not preview.ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=preview.detail)

    # 6. Persist the pending change request + its audit row, then commit.
    cr = await create_change_request(
        db,
        user=user,
        request=request,
        operation=operation.name,
        resource_type=resource_type,
        resource_id=_resource_id_from_args(args),
        resource_display=preview.preview_text[:500],
        args=args.model_dump(mode="json"),
        preview_text=preview.preview_text,
        risk_reason=policy.name or f"{action}:{resource_type}",
        ttl_hours=policy.ttl_hours,
    )
    await db.commit()
    logger.info(
        "approval_gate.queued",
        change_request_id=str(cr.id),
        operation=operation.name,
        resource_type=resource_type,
        policy_id=str(policy.id),
        requested_by=str(user.id),
    )
    return ChangeRequestPending(
        change_request_id=cr.id, state="pending", preview_text=cr.preview_text
    )


__all__ = ["ChangeRequestPending", "OPERATION_ACTION", "gate_or_execute"]
