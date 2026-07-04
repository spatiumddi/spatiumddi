"""DNSBL / RBL reputation monitoring (#528)

Schema for the DNS blocklist reputation sweep:

* ``dnsbl_list`` — the curated catalog of blocklists (one row per list),
  seeded as platform rows (``is_builtin=True``) at startup the same way
  the BGP-communities catalog is. Per-list ``enabled``, DNS ``zone_suffix``
  (``zen.spamhaus.org``), ``return_codes`` map, ``requires_registration``
  + ``qps_note``.
* ``dnsbl_pinned_ip`` — operator-pinned IPs to always monitor.
* ``dnsbl_listing`` — per-(ip, list) result / latch state.

Plus four ``PlatformSettings`` columns gating the daily sweep, and the
``security.dnsbl`` feature-module seed (#14). The catalog rows are seeded
at startup (``seed_dnsbl_catalog`` in app.services.dnsbl.catalog) — that's
the established pattern for curated catalogs (idempotent, keyed on
``zone_suffix``). The ``ip_blocklisted`` alert rule is likewise seeded at
startup (``seed_ip_blocklisted_alert_rule`` in app.services.alerts).

Revision ID: c9f2e1a4d7b6
Revises: a1c7f3e9b284
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9f2e1a4d7b6"
down_revision: str | None = "a1c7f3e9b284"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── PlatformSettings sweep gate + cadence ───────────────────────────
    op.add_column(
        "platform_settings",
        sa.Column(
            "dnsbl_monitoring_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dnsbl_check_interval_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column("dnsbl_sweep_last_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dnsbl_query_resolvers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # ── dnsbl_list (curated catalog) ────────────────────────────────────
    op.create_table(
        "dnsbl_list",
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
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("zone_suffix", sa.String(length=255), nullable=False),
        sa.Column(
            "category",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'combined'"),
        ),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("homepage_url", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "return_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "requires_registration",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("qps_note", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("zone_suffix", name="uq_dnsbl_list_zone_suffix"),
    )
    op.create_index("ix_dnsbl_list_enabled", "dnsbl_list", ["enabled"])

    # ── dnsbl_pinned_ip ─────────────────────────────────────────────────
    op.create_table(
        "dnsbl_pinned_ip",
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
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["ip_address_id"], ["ip_address.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip", name="uq_dnsbl_pinned_ip"),
    )
    op.create_index("ix_dnsbl_pinned_ip_ip_address_id", "dnsbl_pinned_ip", ["ip_address_id"])

    # ── dnsbl_listing (per-ip-per-list result + latch) ──────────────────
    op.create_table(
        "dnsbl_listing",
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
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("list_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("listed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("source", sa.String(length=20), nullable=False, server_default=sa.text("'ipam'")),
        sa.Column(
            "return_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("txt_reason", sa.Text(), nullable=True),
        sa.Column("check_error", sa.Text(), nullable=True),
        sa.Column("first_listed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["list_id"], ["dnsbl_list.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip", "list_id", name="uq_dnsbl_listing_ip_list"),
    )
    op.create_index("ix_dnsbl_listing_list_id", "dnsbl_listing", ["list_id"])
    op.create_index("ix_dnsbl_listing_ip", "dnsbl_listing", ["ip"])
    op.create_index(
        "ix_dnsbl_listing_listed",
        "dnsbl_listing",
        ["listed"],
        postgresql_where=sa.text("listed IS TRUE"),
    )

    # ── feature_module seed (non-negotiable #14) ────────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('security.dnsbl', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'security.dnsbl'"))
    op.drop_index("ix_dnsbl_listing_listed", table_name="dnsbl_listing")
    op.drop_index("ix_dnsbl_listing_ip", table_name="dnsbl_listing")
    op.drop_index("ix_dnsbl_listing_list_id", table_name="dnsbl_listing")
    op.drop_table("dnsbl_listing")
    op.drop_index("ix_dnsbl_pinned_ip_ip_address_id", table_name="dnsbl_pinned_ip")
    op.drop_table("dnsbl_pinned_ip")
    op.drop_index("ix_dnsbl_list_enabled", table_name="dnsbl_list")
    op.drop_table("dnsbl_list")
    op.drop_column("platform_settings", "dnsbl_query_resolvers")
    op.drop_column("platform_settings", "dnsbl_sweep_last_run_at")
    op.drop_column("platform_settings", "dnsbl_check_interval_hours")
    op.drop_column("platform_settings", "dnsbl_monitoring_enabled")
