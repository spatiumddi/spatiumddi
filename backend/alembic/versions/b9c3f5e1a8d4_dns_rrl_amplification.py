"""DNS BIND9 RRL + amplification options (#146 Phase 1)

Adds Response Rate Limiting + amplification-reduction knobs to
``dns_server_options``. Every column carries a no-op server_default so
existing groups render byte-identical named.conf until an operator opts in.

Revision ID: b9c3f5e1a8d4
Revises: c7d1f04e9a2b
Create Date: 2026-06-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b9c3f5e1a8d4"
down_revision: str | None = "c7d1f04e9a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dns_server_options",
        sa.Column("rrl_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "rrl_responses_per_second",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("rrl_window", sa.Integer(), nullable=False, server_default=sa.text("15")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("rrl_slip", sa.Integer(), nullable=False, server_default=sa.text("2")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("rrl_qps_scale", sa.Integer(), nullable=True),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "rrl_exempt_clients",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "rrl_log_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "minimal_responses",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("tcp_clients", sa.Integer(), nullable=True),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("clients_per_query", sa.Integer(), nullable=True),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("max_clients_per_query", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dns_server_options", "max_clients_per_query")
    op.drop_column("dns_server_options", "clients_per_query")
    op.drop_column("dns_server_options", "tcp_clients")
    op.drop_column("dns_server_options", "minimal_responses")
    op.drop_column("dns_server_options", "rrl_log_only")
    op.drop_column("dns_server_options", "rrl_exempt_clients")
    op.drop_column("dns_server_options", "rrl_qps_scale")
    op.drop_column("dns_server_options", "rrl_slip")
    op.drop_column("dns_server_options", "rrl_window")
    op.drop_column("dns_server_options", "rrl_responses_per_second")
    op.drop_column("dns_server_options", "rrl_enabled")
