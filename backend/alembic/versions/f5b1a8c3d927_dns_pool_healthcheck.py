"""dns_pool_healthcheck

Revision ID: f5b1a8c3d927
Revises: e7f218ac4d9b
Create Date: 2026-04-29 11:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "f5b1a8c3d927"
down_revision: Union[str, None] = "e7f218ac4d9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dns_pool",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("zone_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("record_name", sa.String(length=255), nullable=False),
        sa.Column(
            "record_type", sa.String(length=10), nullable=False, server_default="A"
        ),
        sa.Column("ttl", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "hc_type", sa.String(length=10), nullable=False, server_default="tcp"
        ),
        sa.Column("hc_target_port", sa.Integer(), nullable=True),
        sa.Column(
            "hc_path", sa.String(length=255), nullable=False, server_default="/"
        ),
        sa.Column(
            "hc_method", sa.String(length=10), nullable=False, server_default="GET"
        ),
        sa.Column(
            "hc_verify_tls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "hc_expected_status_codes",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[200,201,202,204,301,302,304]'::jsonb"),
        ),
        sa.Column(
            "hc_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column(
            "hc_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "hc_unhealthy_threshold",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
        sa.Column(
            "hc_healthy_threshold",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["zone_id"], ["dns_zone.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "zone_id", "record_name", name="uq_dns_pool_zone_record"
        ),
    )
    op.create_index("ix_dns_pool_zone", "dns_pool", ["zone_id"], unique=False)
    op.create_index(
        "ix_dns_pool_next_check_at", "dns_pool", ["next_check_at"], unique=False
    )

    op.create_table(
        "dns_pool_member",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("pool_id", sa.UUID(), nullable=False),
        sa.Column("address", sa.String(length=45), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "last_check_state",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_error", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "consecutive_successes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
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
        sa.ForeignKeyConstraint(["pool_id"], ["dns_pool.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pool_id", "address", name="uq_dns_pool_member_addr"),
    )
    op.create_index(
        "ix_dns_pool_member_pool", "dns_pool_member", ["pool_id"], unique=False
    )

    op.add_column(
        "dns_record",
        sa.Column("pool_member_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_dns_record_pool_member",
        "dns_record",
        "dns_pool_member",
        ["pool_member_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_dns_record_pool_member_id",
        "dns_record",
        ["pool_member_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dns_record_pool_member_id", table_name="dns_record")
    op.drop_constraint("fk_dns_record_pool_member", "dns_record", type_="foreignkey")
    op.drop_column("dns_record", "pool_member_id")

    op.drop_index("ix_dns_pool_member_pool", table_name="dns_pool_member")
    op.drop_table("dns_pool_member")

    op.drop_index("ix_dns_pool_next_check_at", table_name="dns_pool")
    op.drop_index("ix_dns_pool_zone", table_name="dns_pool")
    op.drop_table("dns_pool")
