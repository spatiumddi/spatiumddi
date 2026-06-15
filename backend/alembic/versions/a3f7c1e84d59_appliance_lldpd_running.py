"""appliance.lldpd_running — persist the LLDP daemon up/down bool (#430)

The supervisor has shipped ``lldpd_running`` in every heartbeat since #347
(appliance_state lldpd sidecar), but the backend had no field / column /
handler, so ``extra="ignore"`` silently dropped it (a #428-shape defect).
This adds the column so it persists alongside the already-wired
``lldp_neighbours`` set — an empty neighbour set is "no neighbours" only
when lldpd is up, "lldpd down" otherwise.

Revision ID: a3f7c1e84d59
Revises: f4a1c8e92b07
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a3f7c1e84d59"
down_revision: str | None = "f4a1c8e92b07"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("lldpd_running", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "lldpd_running")
