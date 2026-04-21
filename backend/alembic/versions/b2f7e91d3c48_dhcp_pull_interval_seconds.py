"""DHCP lease-pull interval: minutes → seconds (default 15s).

Revision ID: b2f7e91d3c48
Revises: a5b8e9c31f42
Create Date: 2026-04-20 18:00:00

Enables near-real-time IPAM population from Windows DHCP. The beat
schedule ticks every 10s now; the task still gates on this setting
so the UI can change cadence without restarting beat. A value of
15 means "poll every 15 seconds".

Column rename + value conversion:
  dhcp_pull_leases_interval_minutes  →  dhcp_pull_leases_interval_seconds
  existing `minutes` are multiplied by 60 to preserve current cadence.
  server_default drops to "15" for new rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b2f7e91d3c48"
down_revision: str | None = "a5b8e9c31f42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename first, then convert existing values (minutes × 60 → seconds).
    # Anyone with the old default of 5 ends up at 300s, which is safe — the
    # UI surface mentions the new 15s default so operators who want faster
    # polling can opt in.
    op.alter_column(
        "platform_settings",
        "dhcp_pull_leases_interval_minutes",
        new_column_name="dhcp_pull_leases_interval_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("5"),
        server_default=sa.text("15"),
    )
    op.execute(
        "UPDATE platform_settings SET dhcp_pull_leases_interval_seconds = "
        "dhcp_pull_leases_interval_seconds * 60"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE platform_settings SET dhcp_pull_leases_interval_seconds = "
        "GREATEST(1, dhcp_pull_leases_interval_seconds / 60)"
    )
    op.alter_column(
        "platform_settings",
        "dhcp_pull_leases_interval_seconds",
        new_column_name="dhcp_pull_leases_interval_minutes",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("15"),
        server_default=sa.text("5"),
    )
