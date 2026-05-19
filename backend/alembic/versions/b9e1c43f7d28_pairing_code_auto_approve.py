"""pairing_code.auto_approve — self-bootstrap codes auto-approve (#272 Phase 1)

Adds ``auto_approve`` (Boolean, default false) to the
``pairing_code`` table. The /self-register-bootstrap endpoint sets
it True; /supervisor/register reads it and runs the cert-signing +
state-flip path inline so the operator doesn't have to manually
approve their own local supervisor on full-stack / frontend-core
appliances.

Operator-typed codes from the Fleet → Pairing tab keep the
default ``false`` so manual approval stays the norm for any
remote pairing.

Revision ID: b9e1c43f7d28
Revises: a8d2e91f5c47
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b9e1c43f7d28"
down_revision = "a8d2e91f5c47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pairing_code",
        sa.Column(
            "auto_approve",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pairing_code", "auto_approve")
