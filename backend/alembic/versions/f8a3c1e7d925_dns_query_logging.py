"""Add DNS query logging options to dns_server_options

Revision ID: f8a3c1e7d925
Revises: e7f3a1c9b5d8
Create Date: 2026-04-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "f8a3c1e7d925"
down_revision = "e7f3a1c9b5d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_server_options",
        sa.Column("query_log_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("query_log_channel", sa.String(length=20), nullable=False, server_default="file"),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "query_log_file",
            sa.String(length=500),
            nullable=False,
            server_default="/var/log/named/queries.log",
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("query_log_severity", sa.String(length=20), nullable=False, server_default="info"),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "query_log_print_category",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "query_log_print_severity",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "query_log_print_time",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_options", "query_log_print_time")
    op.drop_column("dns_server_options", "query_log_print_severity")
    op.drop_column("dns_server_options", "query_log_print_category")
    op.drop_column("dns_server_options", "query_log_severity")
    op.drop_column("dns_server_options", "query_log_file")
    op.drop_column("dns_server_options", "query_log_channel")
    op.drop_column("dns_server_options", "query_log_enabled")
