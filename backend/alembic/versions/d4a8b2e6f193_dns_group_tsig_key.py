"""Add per-group TSIG key for RFC 2136 dynamic updates.

Revision ID: d4a8b2e6f193
Revises: c3f7e5a9b2d1
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4a8b2e6f193"
down_revision = "c3f7e5a9b2d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_server_group",
        sa.Column("tsig_key_name", sa.String(255), nullable=True),
    )
    op.add_column(
        "dns_server_group",
        sa.Column("tsig_key_secret", sa.String(255), nullable=True),
    )
    op.add_column(
        "dns_server_group",
        sa.Column(
            "tsig_key_algorithm",
            sa.String(50),
            nullable=False,
            server_default="hmac-sha256",
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_group", "tsig_key_algorithm")
    op.drop_column("dns_server_group", "tsig_key_secret")
    op.drop_column("dns_server_group", "tsig_key_name")
