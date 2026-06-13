"""#404 — firewall_logging_enabled platform setting

Adds the opt-in firewall-logging master switch. When on (and firewall_enabled
is on), the rendered nft drop-in carries a rate-limited catch-all
``log prefix "spatium-fw: "`` so dropped packets land in the kernel log for the
Firewall → Logs realtime viewer.

Revision ID: c5f1a2b3d4e6
Revises: b3e7d1f9a204
Create Date: 2026-06-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c5f1a2b3d4e6"
down_revision = "b3e7d1f9a204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "firewall_logging_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "firewall_logging_enabled")
