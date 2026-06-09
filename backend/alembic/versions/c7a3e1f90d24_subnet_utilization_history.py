"""subnet_utilization_history — per-subnet daily IP-utilization snapshots (#44).

Powers the 30 / 90-day "% used over time" chart on the subnet detail. A
daily beat task records ``Subnet.allocated_ips`` / ``total_ips`` per subnet
and prunes rows older than 90 days, so the table stays bounded.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "c7a3e1f90d24"
down_revision: str | None = "a3f1e9c47b20"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "subnet_utilization_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subnet_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subnet.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allocated_ips", sa.Integer(), nullable=False),
        sa.Column("total_ips", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_subnet_util_hist_subnet_sampled",
        "subnet_utilization_history",
        ["subnet_id", "sampled_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subnet_util_hist_subnet_sampled",
        table_name="subnet_utilization_history",
    )
    op.drop_table("subnet_utilization_history")
