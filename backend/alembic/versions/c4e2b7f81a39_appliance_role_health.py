"""Appliance.role_health JSONB — service-container watchdog payload.

#170 Wave E. Free-form ``{<compose-service>: {role, status, since,
container_id}}`` dict the supervisor reports on every heartbeat; the
Fleet drilldown surfaces per-service chips driven by the ``status``
field. Default ``'{}'::jsonb`` so existing rows backfill cleanly + a
fresh pair-but-no-roles appliance reports empty without nullable
gymnastics on the read path.

Revision ID: c4e2b7f81a39
Revises: 66471b4aa8e0
Create Date: 2026-05-15 17:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4e2b7f81a39"
down_revision = "66471b4aa8e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "role_health",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "role_health")
