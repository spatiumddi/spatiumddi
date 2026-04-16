"""DNS blocking lists, entries, exceptions and association tables.

Revision ID: c3f1e7b92a5d
Revises: b7e3a1f4c8d2
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3f1e7b92a5d"
down_revision = "b7e3a1f4c8d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dns_blocklist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True, index=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("category", sa.String(50), nullable=False, server_default="custom"),
        sa.Column("source_type", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("feed_url", sa.String(1024), nullable=True),
        sa.Column("feed_format", sa.String(20), nullable=False, server_default="hosts"),
        sa.Column("update_interval_hours", sa.Integer, nullable=False, server_default="24"),
        sa.Column("block_mode", sa.String(20), nullable=False, server_default="nxdomain"),
        sa.Column("sinkhole_ip", sa.String(45), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(50), nullable=True),
        sa.Column("last_sync_error", sa.Text, nullable=True),
        sa.Column("entry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "dns_blocklist_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "list_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("domain", sa.String(512), nullable=False),
        sa.Column("entry_type", sa.String(20), nullable=False, server_default="block"),
        sa.Column("target", sa.String(512), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("is_wildcard", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("source_line", sa.Text, nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("list_id", "domain", name="uq_dns_blocklist_entry_list_domain"),
    )
    op.create_index(
        "ix_dns_blocklist_entry_list_domain", "dns_blocklist_entry", ["list_id", "domain"]
    )
    op.create_index("ix_dns_blocklist_entry_domain", "dns_blocklist_entry", ["domain"])

    op.create_table(
        "dns_blocklist_exception",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "list_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("domain", sa.String(512), nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("list_id", "domain", name="uq_dns_blocklist_exception_list_domain"),
    )
    op.create_index(
        "ix_dns_blocklist_exception_list_domain", "dns_blocklist_exception", ["list_id", "domain"]
    )

    op.create_table(
        "dns_blocklist_group_assoc",
        sa.Column(
            "blocklist_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dns_server_group.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "dns_blocklist_view_assoc",
        sa.Column(
            "blocklist_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "view_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dns_view.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("dns_blocklist_view_assoc")
    op.drop_table("dns_blocklist_group_assoc")
    op.drop_index("ix_dns_blocklist_exception_list_domain", table_name="dns_blocklist_exception")
    op.drop_table("dns_blocklist_exception")
    op.drop_index("ix_dns_blocklist_entry_domain", table_name="dns_blocklist_entry")
    op.drop_index("ix_dns_blocklist_entry_list_domain", table_name="dns_blocklist_entry")
    op.drop_table("dns_blocklist_entry")
    op.drop_table("dns_blocklist")
