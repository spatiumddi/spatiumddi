"""Issue #285 Phase 2 — firewall_apply_state + platform_settings.firewall_enabled.

Phase 2b foundation, all additive:

* ``firewall_apply_state`` — one row per appliance, mirroring back what the
  host-side spatium-firewall-reload runner writes (applied hash / status /
  base-conf marker) plus the control-plane's rendered hash. The full column
  set (incl. the 2c test-apply + 2d stalled-watermark fields) lands now so
  the table migrates exactly once.
* ``platform_settings.firewall_enabled`` — the Phase-2a master switch
  (default FALSE; the server-side-authoritative render is opt-in).

Revision ID: a3f1d9e07c52
Revises: d7e2a4f9c1b3
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "a3f1d9e07c52"
down_revision: str | None = "d7e2a4f9c1b3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "firewall_apply_state",
        sa.Column("appliance_id", UUID(as_uuid=True), nullable=False),
        sa.Column("rendered_hash", sa.String(length=64), nullable=True),
        sa.Column("applied_hash", sa.String(length=64), nullable=True),
        sa.Column("applied_status", sa.String(length=48), nullable=True),
        sa.Column("base_conf_marker", sa.String(length=64), nullable=True),
        sa.Column("last_rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_confirmed_hash", sa.String(length=64), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "pending_commit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("commit_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stalled_since", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["appliance_id"], ["appliance.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("appliance_id"),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "firewall_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "firewall_enabled")
    op.drop_table("firewall_apply_state")
