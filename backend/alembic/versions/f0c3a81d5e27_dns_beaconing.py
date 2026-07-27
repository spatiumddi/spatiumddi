"""dns threat: C2 beaconing score on the per-client rollup

Revision ID: f0c3a81d5e27
Revises: e5b2f09c71a4
Create Date: 2026-07-26 15:00:00.000000

Issue #699, second detection.

Scored onto the existing ``dns_client_window`` rather than a table of
its own — that rollup was built with detection-agnostic feature columns
precisely so a second heuristic could ride it without a second
aggregation pass over the query log.

``beacon_candidates`` carries the query names that scored, not just a
number. A monitoring agent and a C2 callback are indistinguishable by
timing alone, so the evidence has to let an operator recognise their
own infrastructure: "metrics.corp.example.com every 30 s" is an
instant dismissal where a bare score is not.

Additive with server defaults, so existing rows read as
"no beaconing observed" until the next rollup recomputes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "f0c3a81d5e27"
down_revision = "e5b2f09c71a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_client_window",
        sa.Column(
            "beacon_score", sa.Float(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "dns_client_window",
        sa.Column(
            "beacon_candidates",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dns_client_window",
        sa.Column(
            "beacon_detail", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
    )
    # Same access pattern the tunnel index serves: "worst offenders
    # recently", for the Threat tab and the alert matcher.
    op.create_index(
        "ix_dns_client_window_beacon",
        "dns_client_window",
        ["window_start", "beacon_score"],
    )


def downgrade() -> None:
    op.drop_index("ix_dns_client_window_beacon", table_name="dns_client_window")
    op.drop_column("dns_client_window", "beacon_detail")
    op.drop_column("dns_client_window", "beacon_candidates")
    op.drop_column("dns_client_window", "beacon_score")
