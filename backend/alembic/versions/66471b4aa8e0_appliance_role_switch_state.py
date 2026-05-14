"""Issue #170 Wave D follow-up — role_switch_state + reason on appliance.

Captures the outcome of the supervisor's compose-lifecycle apply
after a role assignment lands. The supervisor's
``service_lifecycle.apply_role_assignment()`` returns one of:

* ``idle`` — nothing assigned / nothing running (clean state).
* ``ready`` — desired services running, stopped services stopped.
* ``failed`` — compose up / stop returned non-zero. ``role_switch_
  reason`` carries the first stderr line for the operator's UI.

The Fleet UI's role-assignment section renders a red banner when
``role_switch_state="failed"`` so the operator sees the failure
without SSH-ing into the appliance.

Revision ID: 66471b4aa8e0
Revises: 076e76856742
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "66471b4aa8e0"
down_revision: str | None = "076e76856742"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("role_switch_state", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("role_switch_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "role_switch_reason")
    op.drop_column("appliance", "role_switch_state")
