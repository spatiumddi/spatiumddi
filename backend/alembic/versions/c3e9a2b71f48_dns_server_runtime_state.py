"""dns_server_runtime_state

Revision ID: c3e9a2b71f48
Revises: f5b1a8c3d927
Create Date: 2026-04-29 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "c3e9a2b71f48"
down_revision: Union[str, None] = "f5b1a8c3d927"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dns_server_runtime_state",
        sa.Column("server_id", sa.UUID(), nullable=False),
        sa.Column(
            "rendered_files",
            JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rndc_status_text", sa.Text(), nullable=True),
        sa.Column("rndc_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["server_id"], ["dns_server.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("server_id"),
    )


def downgrade() -> None:
    op.drop_table("dns_server_runtime_state")
