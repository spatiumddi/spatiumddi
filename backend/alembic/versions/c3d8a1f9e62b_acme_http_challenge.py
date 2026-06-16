"""ACME client — http-01 challenge token store (#438 Phase 4)

Cluster-global token → key-authorization mapping the unauthenticated
``/.well-known/acme-challenge/<token>`` endpoint reads, so http-01 works
behind the MetalLB VIP fronting N frontend replicas (per-pod memory
wouldn't). Rows are written before telling the CA to validate and deleted
after.

Revision ID: c3d8a1f9e62b
Revises: b2f5a9c41e07
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3d8a1f9e62b"
down_revision: str | None = "b2f5a9c41e07"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "acme_http_challenge",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("key_authorization", sa.Text(), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["acme_order.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_acme_http_challenge_token", "acme_http_challenge", ["token"], unique=True)
    op.create_index("ix_acme_http_challenge_order_id", "acme_http_challenge", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_acme_http_challenge_order_id", table_name="acme_http_challenge")
    op.drop_index("ix_acme_http_challenge_token", table_name="acme_http_challenge")
    op.drop_table("acme_http_challenge")
