"""Self-service request portal — origin + justification on change_request (#696)

#62 shipped the approval engine as a *brake*: a two-person rule gating risky
deletes. #696 points the same machinery the other way — a low-privilege user
asks for something they cannot do themselves ("a /26 in Site X", "an A record
for my app"), an approver reviews it, and approving **executes the
provisioning**.

Mechanically those are the same lifecycle: a frozen operation + args, a pending
row, a second person who must hold the operation's own permission, a re-preview
stale-guard, then ``apply()`` under the approver. So rather than grow a second
state machine (explicitly called out in #696), this reuses ``change_request``
and adds two columns:

``origin``
    ``'gate'``   — created by the #62 gate intercepting a risky operation.
    ``'portal'`` — submitted through the #696 self-service portal.

    Existing rows backfill to ``'gate'``, and ``/change-requests`` filters to
    ``origin='gate'`` while ``/requests`` filters to ``'portal'``, so neither
    queue is polluted by the other and #62's surface is byte-identical to
    before.

``justification``
    Free text from the requester ("why do you need this?"). NULL on gate rows —
    the gate has ``risk_reason`` instead, which is policy-derived rather than
    human-written.

The index on ``(origin, state)`` backs the portal queue read, which is always
scoped to one origin and usually to ``state='pending'``.

Revision ID: a4f81c26b9e3
Revises: f3b6d914c072
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a4f81c26b9e3"
down_revision: str | None = "f3b6d914c072"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # server_default so the backfill of existing rows is implicit and the
    # column can be NOT NULL in one step. Kept on the column afterwards: the
    # gate never passes origin explicitly, so 'gate' staying the database
    # default is what makes #62's inserts unchanged.
    op.add_column(
        "change_request",
        sa.Column(
            "origin",
            sa.String(length=16),
            nullable=False,
            server_default="gate",
        ),
    )
    op.add_column(
        "change_request",
        sa.Column("justification", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_change_request_origin_state",
        "change_request",
        ["origin", "state"],
    )

    # ── feature_module seed (non-negotiable #14) — default-off ───────────
    # The portal is a delegation model: it lets people outside the network
    # team put work into an approver's queue. That is an organisational
    # decision, so an operator opts in rather than finding the surface
    # already live after an upgrade.
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('governance.requests', FALSE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'governance.requests'"))
    op.drop_index("ix_change_request_origin_state", table_name="change_request")
    op.drop_column("change_request", "justification")
    op.drop_column("change_request", "origin")
