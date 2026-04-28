"""dhcp_option_templates

Revision ID: e7f218ac4d9b
Revises: d8e4a73f12c5
Create Date: 2026-04-28 22:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "e7f218ac4d9b"
down_revision: Union[str, None] = "d8e4a73f12c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dhcp_option_template",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "address_family",
            sa.String(length=4),
            nullable=False,
            server_default="ipv4",
        ),
        sa.Column(
            "options",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
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
            ["group_id"], ["dhcp_server_group.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "name", name="uq_dhcp_option_template_group_name"
        ),
    )
    op.create_index(
        "ix_dhcp_option_template_group",
        "dhcp_option_template",
        ["group_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dhcp_option_template_group", table_name="dhcp_option_template")
    op.drop_table("dhcp_option_template")
