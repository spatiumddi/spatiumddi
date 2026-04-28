"""subnet_plan table

Revision ID: c8e1f04a932d
Revises: b9e4d2a17c83
Create Date: 2026-04-29 12:00:00.000000

Operator-designed multi-level CIDR plans, applied transactionally.
The ``tree`` column carries the JSON node hierarchy (see
``app.models.ipam.SubnetPlan`` for the shape). ``applied_at`` flips
the plan to read-only once materialised; ``applied_resource_ids``
captures the IDs of the blocks + subnets that were created so
operators can audit the result.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8e1f04a932d"
down_revision: str | None = "b9e4d2a17c83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subnet_plan",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tree", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_resource_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["ip_space.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subnet_plan_space_id", "subnet_plan", ["space_id"])
    op.create_index("ix_subnet_plan_name", "subnet_plan", ["name"])


def downgrade() -> None:
    op.drop_index("ix_subnet_plan_name", table_name="subnet_plan")
    op.drop_index("ix_subnet_plan_space_id", table_name="subnet_plan")
    op.drop_table("subnet_plan")
