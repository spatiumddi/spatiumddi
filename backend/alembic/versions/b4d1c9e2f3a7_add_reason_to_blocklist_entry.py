"""add reason to dns_blocklist_entry

Revision ID: b4d1c9e2f3a7
Revises: fe6715916c27
Create Date: 2026-04-16 03:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b4d1c9e2f3a7"
down_revision: Union[str, None] = "fe6715916c27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dns_blocklist_entry",
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("dns_blocklist_entry", "reason")
