"""packet_capture table + pcap_retention_days + tools.pcap feature module (#59)

The persisted job model for on-demand tcpdump capture (issue #59),
mirroring ``nmap_scan`` but with a binary ``.pcap`` artifact on disk
(``pcap_path``) instead of inline XML/text, a vantage discriminator
(server / appliance), and live byte/packet progress columns. Plus:

* ``platform_settings.pcap_retention_days`` (default 7) — the nightly
  prune deletes terminal rows + unlinks their .pcap files past this.
* the ``tools.pcap`` feature-module seed (default-enabled) so the whole
  ``/api/v1/pcap`` surface + sidebar entry gate behind one toggle
  (non-negotiable #14).

Revision ID: b8e3f1c47a92
Revises: a3f7c1e84d59
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8e3f1c47a92"
down_revision: str | None = "a3f7c1e84d59"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "packet_capture",
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
        # vantage
        sa.Column("vantage_kind", sa.String(length=16), nullable=False, server_default="server"),
        sa.Column("appliance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vantage_label", sa.String(length=255), nullable=False, server_default=""),
        # inputs
        sa.Column("interface", sa.String(length=64), nullable=True),
        sa.Column("bpf_filter", sa.Text(), nullable=True),
        sa.Column("snaplen", sa.Integer(), nullable=False, server_default="256"),
        sa.Column("promiscuous", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("max_packets", sa.Integer(), nullable=True),
        sa.Column("max_duration_s", sa.Integer(), nullable=True),
        sa.Column("max_bytes", sa.BigInteger(), nullable=True),
        # run state
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("command_line", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("tcpdump_pid", sa.Integer(), nullable=True),
        # progress
        sa.Column("packets_captured", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_captured", sa.BigInteger(), nullable=False, server_default="0"),
        # artifact
        sa.Column("pcap_path", sa.Text(), nullable=True),
        sa.Column("pcap_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("pcap_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "artifact_missing", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # provenance
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["appliance_id"], ["appliance.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_packet_capture_status", "packet_capture", ["status"])
    op.create_index("ix_packet_capture_appliance", "packet_capture", ["appliance_id"])
    op.create_index(
        "ix_packet_capture_creator_created",
        "packet_capture",
        ["created_by_user_id", "created_at"],
    )
    op.create_index("ix_packet_capture_created", "packet_capture", ["created_at"])

    op.add_column(
        "platform_settings",
        sa.Column("pcap_retention_days", sa.Integer(), nullable=False, server_default="7"),
    )

    # ── feature_module seed (non-negotiable #14) ────────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('tools.pcap', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'tools.pcap'"))
    op.drop_column("platform_settings", "pcap_retention_days")
    op.drop_index("ix_packet_capture_created", table_name="packet_capture")
    op.drop_index("ix_packet_capture_creator_created", table_name="packet_capture")
    op.drop_index("ix_packet_capture_appliance", table_name="packet_capture")
    op.drop_index("ix_packet_capture_status", table_name="packet_capture")
    op.drop_table("packet_capture")
