"""Utilization max-prefix settings (exclude small PTP subnets).

Revision ID: d4e2f7a9b1c6
Revises: c8d5e2a9f736
Create Date: 2026-04-21 12:00:00

Adds ``utilization_max_prefix_ipv4`` / ``_ipv6`` to PlatformSettings so
operators can suppress /30, /31, /127 etc. from the dashboard heatmap
and (incoming) alerts framework. A subnet is excluded when its prefix
length is strictly larger than the configured max — set to 32 / 128 to
disable the filter entirely.

Defaults: 29 (v4 — excludes /30..32) and 126 (v6 — excludes /127..128).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d4e2f7a9b1c6"
down_revision: str | None = "c8d5e2a9f736"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "utilization_max_prefix_ipv4",
            sa.Integer(),
            nullable=False,
            server_default="29",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "utilization_max_prefix_ipv6",
            sa.Integer(),
            nullable=False,
            server_default="126",
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "utilization_max_prefix_ipv6")
    op.drop_column("platform_settings", "utilization_max_prefix_ipv4")
