"""Issue #170 Phase E2 — appliance.port_conflicts JSONB.

Lands the schema half of the DHCP bridged-mode port-conflict
pre-flight. The supervisor probes ``ss -uln 'sport = :67'`` on the
host every heartbeat and reports any conflicting listener in
``port_conflicts``. The control plane persists the block on the
appliance row; the frontend's Fleet drilldown surfaces it as a red
banner on the role-assignment section whenever the operator's
chosen DHCP server-group is in bridged mode.

Shape: ``{"udp_67": "<users-string from ss output>", ...}``. Free-
form JSONB so future ports (e.g. UDP/53 when the supervisor lands
DNS role pre-flight) slot in without a migration.

Revision ID: 076e76856742
Revises: c6c3137e3554
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "076e76856742"
down_revision: str | None = "c6c3137e3554"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "port_conflicts",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "port_conflicts")
