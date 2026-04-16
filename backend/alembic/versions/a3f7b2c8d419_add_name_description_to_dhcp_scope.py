"""add name + description to dhcp_scope

Revision ID: a3f7b2c8d419
Revises: e2a6f3b8c1d4
Create Date: 2026-04-16 18:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3f7b2c8d419"
down_revision: str | None = "e2a6f3b8c1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dhcp_scope",
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("dhcp_scope", "description")
    op.drop_column("dhcp_scope", "name")
