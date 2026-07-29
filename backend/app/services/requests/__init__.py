"""Self-service request portal (issue #696).

#62 shipped the approval engine as a *brake* — a two-person rule gating risky
deletes. This is the same machinery pointed the other way: a low-privilege
user asks for something they cannot do themselves, an approver reviews it, and
approving **executes the provisioning**.

The package is deliberately small. It contributes a catalog (what may be
asked for) and a submit path (turning a form into a pending row); the
lifecycle, the approver checks, the execution and the audit trail are all
``services.approvals`` unchanged.
"""

from __future__ import annotations

from app.services.requests.catalog import (
    REQUEST_KINDS,
    RequestKind,
    all_kinds,
    get_kind,
    resolve_operation,
)
from app.services.requests.service import (
    MAX_JUSTIFICATION_CHARS,
    PORTAL_REQUEST_TTL_HOURS,
    submit_request,
)

__all__ = [
    "MAX_JUSTIFICATION_CHARS",
    "PORTAL_REQUEST_TTL_HOURS",
    "REQUEST_KINDS",
    "RequestKind",
    "all_kinds",
    "get_kind",
    "resolve_operation",
    "submit_request",
]
