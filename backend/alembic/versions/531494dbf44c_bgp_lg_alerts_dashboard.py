"""bgp looking glass — alert-family columns (issue #566 Phase 5)

Revision ID: 531494dbf44c
Revises: cb279a6afd70
Create Date: 2026-07-05 10:35:27.376300

Two additive columns backing the bgp_lg_* alert family:

* ``subnet.bgp_should_advertise`` — pure operator intent ("this subnet
  is supposed to be carrying traffic over BGP somewhere"), consulted by
  the ``bgp_lg_missing_advertisement`` alert. Partial index (WHERE true)
  since only a small subset of subnets will ever be flagged.
* ``bgp_lg_route.last_flap_at`` — timestamp of the most recent
  absence-withdraw bump on a learned route (routes_ingest.py already
  bumps ``flap_count``; this adds the recency dimension so the
  ``bgp_lg_route_flap`` alert can require a RECENT flap, not just a
  lifetime count, and auto-resolve once the route settles).

No AlertRule schema changes — all six rule types reuse existing
generic columns (route_flap's flap-count floor reuses
``threshold_percent``, same as ``voice_lease_count_below`` /
``stale_ip_count``); the six AlertRule seed rows are inserted by
``seed_bgp_lg_alert_rules()`` at app startup (main.py), not here — see
the alert-seeding convention documented in ``services/alerts.py``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "531494dbf44c"
down_revision: Union[str, None] = "cb279a6afd70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "bgp_should_advertise",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_subnet_bgp_should_advertise",
        "subnet",
        ["bgp_should_advertise"],
        postgresql_where=sa.text("bgp_should_advertise = true"),
    )
    op.add_column(
        "bgp_lg_route",
        sa.Column(
            "last_flap_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_bgp_lg_route_last_flap_at",
        "bgp_lg_route",
        ["last_flap_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_bgp_lg_route_last_flap_at", table_name="bgp_lg_route")
    op.drop_column("bgp_lg_route", "last_flap_at")
    op.drop_index("ix_subnet_bgp_should_advertise", table_name="subnet")
    op.drop_column("subnet", "bgp_should_advertise")
