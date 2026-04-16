"""add auto_from_lease to ip_address

Revision ID: e2a6f3b8c1d4
Revises: b4d1c9e2f3a7
Create Date: 2026-04-16 04:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e2a6f3b8c1d4"
down_revision: Union[str, None] = "b4d1c9e2f3a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ip_address",
        sa.Column(
            "auto_from_lease",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ip_address", "auto_from_lease")
