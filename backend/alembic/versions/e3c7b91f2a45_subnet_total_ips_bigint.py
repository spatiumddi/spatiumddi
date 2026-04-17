"""widen subnet.total_ips to BigInteger for IPv6 /64 support

Revision ID: e3c7b91f2a45
Revises: d7a2b6e9f134
Create Date: 2026-04-16 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3c7b91f2a45"
down_revision: str | None = "d7a2b6e9f134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "subnet",
        "total_ips",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Narrowing back to INT4 will fail if any row has total_ips > 2^31-1.
    op.alter_column(
        "subnet",
        "total_ips",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
