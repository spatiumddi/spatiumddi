"""platform_settings.timezone (#165)

Operator-set IANA timezone, applied to the appliance host via the
supervisor → heartbeat → ``spatium-tz-reload`` runner pipeline that
NTP / SNMP / chrony already use. Empty string means "follow the
install-time default" — the supervisor's heartbeat skips emitting
``desired_timezone`` in that case so the host's existing
``/etc/timezone`` stays in place.

Revision ID: c2e6a89d4b15
Revises: b1d4c9e57f02
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c2e6a89d4b15"
down_revision = "b1d4c9e57f02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "timezone")
