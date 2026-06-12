"""appliance: per-plane host-config apply health (#387)

Adds ``appliance.host_config_health`` — the supervisor's bounded-retry
fire-guard reports, per hash-keyed host-config plane (snmp / ntp / lldp
/ syslog / ssh / resolver / firewall / timezone), whether the desired
config is applied or stuck. ``{<plane>: {state, attempts, at}}`` where
``state`` ∈ {retrying, failing}; only unapplied planes appear, so an
all-healthy box reports ``{}``. Surfaced in the Fleet drilldown so a
stuck apply (the pre-#387 silently-looping NTP bug) is visible.

Revision ID: f4a1c9e7b2d8
Revises: e1c4a9d27b63
Create Date: 2026-06-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f4a1c9e7b2d8"
down_revision: str | None = "e1c4a9d27b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "host_config_health",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "host_config_health")
