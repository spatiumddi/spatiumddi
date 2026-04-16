"""add DNS agent fields and record_op table

Revision ID: b7e3a1f4c8d2
Revises: a1b2c3d4e5f6
Create Date: 2026-04-14 14:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b7e3a1f4c8d2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DNSServer extensions
    op.add_column("dns_server", sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dns_server", sa.Column("agent_jwt_hash", sa.String(128), nullable=True))
    op.add_column("dns_server", sa.Column("agent_fingerprint", sa.String(128), nullable=True))
    op.add_column("dns_server", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dns_server", sa.Column("last_config_etag", sa.String(128), nullable=True))
    op.add_column(
        "dns_server",
        sa.Column("pending_approval", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "dns_server",
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("ix_dns_server_agent_id", "dns_server", ["agent_id"], unique=True)

    # RecordOp queue
    op.create_table(
        "dns_record_op",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("server_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("dns_server.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("zone_name", sa.String(255), nullable=False, index=True),
        sa.Column("op", sa.String(20), nullable=False),  # create | update | delete
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("target_serial", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending", index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dns_record_op_server_state", "dns_record_op", ["server_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_dns_record_op_server_state", table_name="dns_record_op")
    op.drop_table("dns_record_op")
    op.drop_index("ix_dns_server_agent_id", table_name="dns_server")
    for col in (
        "is_primary", "pending_approval", "last_config_etag",
        "last_seen_at", "agent_fingerprint", "agent_jwt_hash", "agent_id",
    ):
        op.drop_column("dns_server", col)
