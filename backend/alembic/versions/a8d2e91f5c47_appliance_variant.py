"""appliance.appliance_variant — installer-role string (#272 Phase 1)

Adds ``appliance_variant`` (``String(32)`` nullable) to the
``appliance`` table so the Fleet UI can categorise rows into
Control plane (full-stack / frontend-core) vs Service agents
(application), and the supervisor's variant-aware label
reconciliation has a persisted source of truth.

NULL on existing rows — the supervisor populates the column on its
next heartbeat once the operator slot-upgrades to a release that
includes the supervisor-side reporter (#272 Phase 1).

Revision ID: a8d2e91f5c47
Revises: 97190c1b0325
Create Date: 2026-05-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a8d2e91f5c47"
down_revision = "97190c1b0325"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("appliance_variant", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "appliance_variant")
