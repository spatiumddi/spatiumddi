"""dns threat: DGA score on the per-client rollup

Revision ID: a4e91c7b3d82
Revises: f0c3a81d5e27
Create Date: 2026-07-27 23:10:00.000000

Issue #699, third detection.

Rides the existing ``dns_client_window`` for the same reason beaconing
did: the rollup's feature columns are detection-agnostic, so a third
heuristic scores off them without a second aggregation pass over the
query log.

``dga_candidates`` carries the implausible registrable domains that made
up the crop, and ``dga_signals`` the per-signal breakdown in the same
wire shape as ``tunnel_signals`` — so the UI renders both detections'
evidence with one component. Both matter for the same reason the
beaconing evidence does: hashed-CDN and shortlink traffic shares the
shape, so an operator has to be able to see WHICH domains scored to
dismiss their own infrastructure.

Note this detection deliberately does NOT use the NXDOMAIN prior the
issue originally specified. ``dns_query_log_entry`` records the
question, never the answer — the BIND9 query log carries no rcode — so
scoring is on name characteristics alone. See ``services/dns_threat/
dga.py`` for the trade-off and the path to layering the per-server
NXDOMAIN metric on later.

Additive with server defaults, so existing rows read as "no DGA
observed" until the next rollup recomputes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a4e91c7b3d82"
down_revision = "f0c3a81d5e27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_client_window",
        sa.Column("dga_score", sa.Float(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "dns_client_window",
        sa.Column(
            "dga_candidates",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dns_client_window",
        sa.Column(
            "dga_signals",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dns_client_window",
        sa.Column("dga_detail", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    # Same access pattern the tunnel + beacon indexes serve: "worst
    # offenders recently", for the Threat tab and the alert matcher.
    op.create_index(
        "ix_dns_client_window_dga",
        "dns_client_window",
        ["window_start", "dga_score"],
    )


def downgrade() -> None:
    op.drop_index("ix_dns_client_window_dga", table_name="dns_client_window")
    op.drop_column("dns_client_window", "dga_detail")
    op.drop_column("dns_client_window", "dga_signals")
    op.drop_column("dns_client_window", "dga_candidates")
    op.drop_column("dns_client_window", "dga_score")
