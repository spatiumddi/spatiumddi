"""dns_tsig_keys

Revision ID: 7c299e8a5490
Revises: a07f6c12e5d3
Create Date: 2026-04-28 20:21:48.856877

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c299e8a5490"
down_revision: Union[str, None] = "a07f6c12e5d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dns_tsig_key",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "algorithm",
            sa.String(length=50),
            nullable=False,
            server_default="hmac-sha256",
        ),
        sa.Column("secret_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["dns_server_group.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "name", name="uq_dns_tsig_key_group_name"),
    )
    op.create_index(
        op.f("ix_dns_tsig_key_group_id"), "dns_tsig_key", ["group_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_dns_tsig_key_group_id"), table_name="dns_tsig_key")
    op.drop_table("dns_tsig_key")
