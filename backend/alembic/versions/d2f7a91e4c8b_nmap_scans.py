"""Nmap scan history.

Revision ID: d2f7a91e4c8b
Revises: c4e7a2f813b9
Create Date: 2026-04-28 12:00:00

Creates the ``nmap_scan`` table backing the on-demand nmap integration.
A row exists for every operator-triggered scan; the ``raw_stdout``
column is appended to in ~2 s flushes by the runner so the SSE stream
endpoint can replay live progress without holding a per-process buffer
on the API node.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d2f7a91e4c8b"
down_revision: str | None = "a4d92f61c08b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nmap_scan",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("target_ip", sa.dialects.postgresql.INET(), nullable=False),
        sa.Column(
            "ip_address_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ip_address.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "preset",
            sa.String(length=32),
            nullable=False,
            server_default="quick",
        ),
        sa.Column("port_spec", sa.String(length=255), nullable=True),
        sa.Column("extra_args", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("command_line", sa.Text(), nullable=True),
        sa.Column("raw_xml", sa.Text(), nullable=True),
        sa.Column("raw_stdout", sa.Text(), nullable=True),
        sa.Column("summary_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
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
    )
    op.create_index(
        "ix_nmap_scan_target_ip_started",
        "nmap_scan",
        ["target_ip", "started_at"],
    )
    op.create_index("ix_nmap_scan_status", "nmap_scan", ["status"])
    op.create_index("ix_nmap_scan_ip_address", "nmap_scan", ["ip_address_id"])


def downgrade() -> None:
    op.drop_index("ix_nmap_scan_ip_address", table_name="nmap_scan")
    op.drop_index("ix_nmap_scan_status", table_name="nmap_scan")
    op.drop_index("ix_nmap_scan_target_ip_started", table_name="nmap_scan")
    op.drop_table("nmap_scan")
