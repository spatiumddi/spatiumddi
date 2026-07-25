"""dns threat analytics: per-client behaviour rollup + query-log client index

Revision ID: d1a7c34e9b60
Revises: c9f4a71b8e35
Create Date: 2026-07-25 17:00:00.000000

Issue #699 — DNS tunneling detection.

``dns_query_log_entry`` is operator triage on a 24 h window, indexed for
"show me this server's recent queries". Threat analytics asks a
different question — "summarise what THIS CLIENT did" — and needs the
answer to outlive the raw rows.

Two pieces:

* ``dns_client_window`` — one row per (client IP, hourly bucket) with
  detection-agnostic behaviour features plus the tunneling verdict.
  Unique on (client_ip, window_start) so the aggregator can upsert a
  bucket as late log lines arrive. Deliberately not per-server: a host
  splitting queries across two resolvers is one suspicious host, not
  two half-suspicious ones.
* ``ix_dns_query_log_client_ts`` on ``dns_query_log_entry`` — the
  aggregator scans by (client_ip, ts) every run, and the existing
  indexes lead with ``server_id`` or ``ts`` alone, so without this the
  rollup degrades to a full scan of a table that can hold a busy
  resolver's entire day.

Additive and inert on upgrade: nothing writes to the new table until an
operator enables the default-off ``security.dns_threat`` module, which
itself does nothing until a DNS group has query logging on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

revision = "d1a7c34e9b60"
down_revision = "c9f4a71b8e35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dns_client_window",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_ip", INET(), nullable=False),
        sa.Column("server_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("query_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("distinct_qnames", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("distinct_parents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("top_parent", sa.String(length=255), nullable=True),
        sa.Column(
            "top_parent_subdomains", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("max_label_length", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "avg_label_length", sa.Float(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "mean_label_entropy", sa.Float(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "payload_qtype_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("tunnel_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "tunnel_signals", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "allowlisted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("client_ip", "window_start", name="uq_dns_client_window_ip_bucket"),
    )
    # "Worst offenders recently" — the alert matcher and the Threat tab.
    op.create_index(
        "ix_dns_client_window_score",
        "dns_client_window",
        ["window_start", "tunnel_score"],
    )
    # Per-client history drilldown.
    op.create_index("ix_dns_client_window_ip", "dns_client_window", ["client_ip"])

    # The aggregator's scan pattern. Without it the rollup full-scans
    # dns_query_log_entry, whose existing indexes lead with server_id
    # or ts and so can't serve "this client, this hour".
    op.create_index(
        "ix_dns_query_log_client_ts",
        "dns_query_log_entry",
        ["client_ip", "ts"],
    )

    # feature_module seed (non-negotiable #14), default-OFF. The module
    # resolves to disabled from the catalog fallback either way, but the
    # table is meant to enumerate the catalog and tooling is entitled to
    # assume it does.
    op.execute(
        sa.text(
            """
            INSERT INTO feature_module (id, enabled)
            VALUES ('security.dns_threat', FALSE)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'security.dns_threat'"))
    op.drop_index("ix_dns_query_log_client_ts", table_name="dns_query_log_entry")
    op.drop_index("ix_dns_client_window_ip", table_name="dns_client_window")
    op.drop_index("ix_dns_client_window_score", table_name="dns_client_window")
    op.drop_table("dns_client_window")
