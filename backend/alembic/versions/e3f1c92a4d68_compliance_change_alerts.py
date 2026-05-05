"""Compliance-change alert rule columns.

Adds three columns to ``alert_rule`` so the new ``compliance_change``
rule type (issue #105) can persist its params + audit-log scan
watermark:

  * classification           — which subnet flag this rule watches
                              (``pci_scope`` | ``hipaa_scope`` |
                              ``internet_facing``).
  * change_scope             — which audit-log actions to react to
                              (``any_change`` | ``create`` | ``delete``).
  * last_scanned_audit_at    — watermark; the evaluator only fires for
                              audit rows whose ``timestamp`` is strictly
                              greater than this. NULL on a fresh rule
                              means "never scanned"; the evaluator
                              stamps it to ``now()`` on its first pass
                              so existing audit history doesn't
                              retroactively page the operator.

All three are nullable — they're only populated for compliance_change
rules. Existing rule rows stay untouched.

Revision ID: e3f1c92a4d68
Revises: c4e8b71f0d23
Create Date: 2026-05-05 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3f1c92a4d68"
down_revision = "c4e8b71f0d23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_rule",
        sa.Column("classification", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "alert_rule",
        sa.Column("change_scope", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "alert_rule",
        sa.Column(
            "last_scanned_audit_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_rule", "last_scanned_audit_at")
    op.drop_column("alert_rule", "change_scope")
    op.drop_column("alert_rule", "classification")
