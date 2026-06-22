"""Two-person approval workflow (issue #62).

The change-gating spine: a risky operation submitted by one operator is
queued as a ``ChangeRequest`` and only executes after a *different*
eligible approver accepts it. Reuses the AI Copilot's
preview/apply ``Operation`` registry — the approve path replays the same
``apply()`` under the approver's identity, after re-running ``preview()``
as a stale-state guard.

Public surface:

* ``policy.match_policy`` — the pure async policy engine deciding
  whether an ``(resource_type, action[, count])`` triggers the gate.
* ``service`` — change-request data access + the guarded state machine
  (create / get / list + the ``mark_*`` transitions), each mutation
  audited.

The gate (``gate.py``) + the API router + the per-handler call sites
land in later slices of #62; this package ships the substrate they call.
"""

from __future__ import annotations

from app.services.approvals.policy import match_policy
from app.services.approvals.service import (
    ChangeRequestStateError,
    DecisionConflict,
    DecisionError,
    DecisionForbidden,
    DecisionNotFound,
    DecisionUnprocessable,
    approve_change_request,
    create_change_request,
    get_change_request,
    list_change_requests,
    mark_approved,
    mark_cancelled,
    mark_executed,
    mark_expired,
    mark_failed,
    mark_rejected,
    reject_change_request,
)

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
    "match_policy",
    "reject_change_request",
]
