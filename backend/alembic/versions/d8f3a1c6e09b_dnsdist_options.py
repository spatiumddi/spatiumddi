"""dnsdist front options on dns_server_options (#146 Phase 2)

Adds the dnsdist-sidecar rate-limit knobs (PowerDNS front). All default to a
no-op (dnsdist_enabled=false) so existing PowerDNS groups are untouched.

Revision ID: d8f3a1c6e09b
Revises: c5a7e2f9b1d6
Create Date: 2026-06-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f3a1c6e09b"
down_revision: str | None = "c5a7e2f9b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dns_server_options",
        sa.Column("dnsdist_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("dnsdist_max_qps_per_client", sa.Integer(), nullable=True),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "dnsdist_action",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'truncate'"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("dnsdist_dynblock_qps", sa.Integer(), nullable=True),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "dnsdist_dynblock_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_options", "dnsdist_dynblock_seconds")
    op.drop_column("dns_server_options", "dnsdist_dynblock_qps")
    op.drop_column("dns_server_options", "dnsdist_action")
    op.drop_column("dns_server_options", "dnsdist_max_qps_per_client")
    op.drop_column("dns_server_options", "dnsdist_enabled")
