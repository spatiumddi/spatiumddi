"""Two-person approval workflow — change requests + approval policies (#62).

A ``ChangeRequest`` is a high-blast-radius operation (a delete, a bulk
op, a factory reset, a large import) that a policy decided needs a
*second* eligible operator to approve before it runs. The requester
submits; the row sits in ``state="pending"``; a different approver who
holds both ``{approve, change_request}`` and the operation's own
``required_permission`` approves; the operation then executes under the
**approver's** identity after re-running its preview (stale-state guard).
The audit trail carries both user IDs — requester on the ``requested``
row, approver on the ``executed`` row with ``old_value.requested_by``.

An ``ApprovalPolicy`` is the operator-tunable rule that decides whether
a given ``(resource_type, action[, count])`` triggers the gate. Built-in
rows seed ``enabled=False`` so existing installs see zero behaviour
change until an operator opts in. The whole surface is gated behind the
``governance.approvals`` feature module (default-off, non-negotiable #14).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Legal lifecycle states for a change request.
#
# ``pending``   — awaiting a second-person decision.
# ``approved``  — an approver accepted; transient until ``executed``/``failed``.
# ``rejected``  — an approver declined; terminal.
# ``executed``  — the operation ran successfully under the approver; terminal.
# ``failed``    — apply() (or its re-preview) failed at execution; terminal.
# ``expired``   — TTL elapsed before a decision; terminal.
# ``cancelled`` — the requester (or a superadmin) withdrew it; terminal.
CHANGE_REQUEST_STATES: frozenset[str] = frozenset(
    {"pending", "approved", "rejected", "executed", "failed", "expired", "cancelled"}
)

# Actions an approval policy can gate. Coarse on purpose (resource_type +
# action [+ count threshold]); per-resource_id scoping is a later
# enhancement that rides #64.
APPROVAL_POLICY_ACTIONS: frozenset[str] = frozenset(
    {"delete", "bulk_delete", "bulk_edit", "bulk_allocate", "factory_reset", "import_commit"}
)


class ChangeRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A risky operation queued for second-person approval."""

    __tablename__ = "change_request"
    __table_args__ = (
        # Pending-queue read + the Celery expiry sweep both scan by
        # (state, expires_at).
        Index("ix_change_request_state_expires", "state", "expires_at"),
        # "my requests" filter.
        Index("ix_change_request_requested_by", "requested_by_user_id"),
        # "what's queued against this resource" lookup.
        Index("ix_change_request_resource", "resource_type", "resource_id"),
    )

    # Operation registry key to replay (e.g. ``delete_subnet``).
    operation: Mapped[str] = mapped_column(String(64), nullable=False)

    # Frozen identity of the target, for filtering + audit. ``resource_id``
    # is NULL for ops with no single target (e.g. factory_reset).
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_display: Mapped[str] = mapped_column(String(500), nullable=False)

    # The operation args to replay on approve, and the preview text + the
    # policy reason both frozen at request time.
    args: Mapped[dict] = mapped_column(JSONB, nullable=False)
    preview_text: Mapped[str] = mapped_column(Text, nullable=False)
    risk_reason: Mapped[str] = mapped_column(String(255), nullable=False)

    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )

    # Requester — FK SET NULL so deleting the user keeps the audit trail;
    # ``requested_by_display`` survives the deletion.
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_by_display: Mapped[str] = mapped_column(String(255), nullable=False)

    # Approver/rejecter — populated on the deciding transition.
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_by_display: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # apply() outcome.
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A rule deciding whether an operation needs second-person approval."""

    __tablename__ = "approval_policy"
    __table_args__ = (
        # The match query filters on (resource_type, action).
        Index("ix_approval_policy_match", "resource_type", "action"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``resource_type`` may be the wildcard ``"*"`` to match any type.
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)

    # Threshold: only require approval when the op touches >= N rows.
    # NULL = always require approval regardless of count.
    min_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # A two-person rule that superadmin bypasses isn't a two-person rule —
    # default True so superadmin also needs a second superadmin.
    applies_to_superadmin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # Request expiry window (default 7 days).
    ttl_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=168, server_default=text("168")
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


__all__ = [
    "APPROVAL_POLICY_ACTIONS",
    "CHANGE_REQUEST_STATES",
    "ApprovalPolicy",
    "ChangeRequest",
]
