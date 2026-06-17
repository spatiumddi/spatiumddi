"""DNS RRL drop counters on dns_metric_sample (#146 Phase 3)

Adds rate_dropped + rate_slipped to the per-minute DNS metric sample so the
server detail Stats tab can chart Response-Rate-Limiting activity and the
dns_rate_limit_dropping alert can evaluate it. Both default 0, so existing
rows + non-RRL servers report no drops.

Revision ID: c5a7e2f9b1d6
Revises: b9c3f5e1a8d4
Create Date: 2026-06-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5a7e2f9b1d6"
down_revision: str | None = "b9c3f5e1a8d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dns_metric_sample",
        sa.Column("rate_dropped", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "dns_metric_sample",
        sa.Column("rate_slipped", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("dns_metric_sample", "rate_slipped")
    op.drop_column("dns_metric_sample", "rate_dropped")
