"""TLS certificate monitoring (#118)

Schema for TLS cert monitoring: ``tls_cert_target`` (what to probe + the
per-row schedule + denormalised latest-known cert identity) and
``tls_cert_probe`` (immutable per-probe history). Plus the per-zone /
per-record ``auto_tls_probe`` opt-in columns, the platform-default probe
cadence, and the ``security.tls_certs`` feature-module seed (#14).

The four ``tls_cert_*`` alert rules are seeded at startup
(``seed_tls_cert_alert_rules`` in app.services.alerts), not here — that's
the established pattern for newer rule kinds (idempotent, keyed on rule_type).

Revision ID: f3e8b1d72a9c
Revises: e1a4b8c92f3d
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3e8b1d72a9c"
down_revision: str | None = "e1a4b8c92f3d"
branch_labels: str | None = None
depends_on: str | None = None


def _identity_columns() -> list[sa.Column]:
    """The cert-identity snapshot columns shared by target + probe."""
    return [
        sa.Column("serial", sa.String(length=128), nullable=True),
        sa.Column("subject_cn", sa.String(length=255), nullable=True),
        sa.Column("issuer_cn", sa.String(length=255), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sans_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("key_algo", sa.String(length=20), nullable=True),
        sa.Column("key_size", sa.Integer(), nullable=True),
        sa.Column("sig_algo", sa.String(length=64), nullable=True),
        sa.Column("chain_depth", sa.Integer(), nullable=True),
        sa.Column("chain_valid", sa.Boolean(), nullable=True),
        sa.Column("chain_error", sa.Text(), nullable=True),
        sa.Column("self_signed", sa.Boolean(), nullable=True),
        sa.Column("fingerprint_sha256", sa.String(length=95), nullable=True),
    ]


def upgrade() -> None:
    # ── opt-in columns on existing tables ───────────────────────────────
    op.add_column(
        "dns_zone",
        sa.Column("auto_tls_probe", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dns_record",
        sa.Column("auto_tls_probe", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "tls_cert_check_interval_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("6"),
        ),
    )

    # ── tls_cert_target ─────────────────────────────────────────────────
    op.create_table(
        "tls_cert_target",
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
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default=sa.text("443")),
        sa.Column("server_name", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("dns_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dns_zone_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("domain_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("interval_hours", sa.Integer(), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        *_identity_columns(),
        sa.ForeignKeyConstraint(["dns_record_id"], ["dns_record.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dns_zone_id"], ["dns_zone.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "host",
            "port",
            "server_name",
            name="uq_tls_cert_target_host_port_sni",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_tls_cert_target_dns_record_id", "tls_cert_target", ["dns_record_id"])
    op.create_index("ix_tls_cert_target_dns_zone_id", "tls_cert_target", ["dns_zone_id"])
    op.create_index("ix_tls_cert_target_domain_id", "tls_cert_target", ["domain_id"])
    op.create_index("ix_tls_cert_target_next_check_at", "tls_cert_target", ["next_check_at"])

    # ── tls_cert_probe ──────────────────────────────────────────────────
    op.create_table(
        "tls_cert_probe",
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
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "probed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        *_identity_columns(),
        sa.Column("leaf_pem", sa.Text(), nullable=True),
        sa.Column("chain_pem", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["target_id"], ["tls_cert_target.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tls_cert_probe_target_probed", "tls_cert_probe", ["target_id", "probed_at"])

    # ── feature_module seed (non-negotiable #14) ────────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('security.tls_certs', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'security.tls_certs'"))
    op.drop_index("ix_tls_cert_probe_target_probed", table_name="tls_cert_probe")
    op.drop_table("tls_cert_probe")
    op.drop_index("ix_tls_cert_target_next_check_at", table_name="tls_cert_target")
    op.drop_index("ix_tls_cert_target_domain_id", table_name="tls_cert_target")
    op.drop_index("ix_tls_cert_target_dns_zone_id", table_name="tls_cert_target")
    op.drop_index("ix_tls_cert_target_dns_record_id", table_name="tls_cert_target")
    op.drop_table("tls_cert_target")
    op.drop_column("platform_settings", "tls_cert_check_interval_hours")
    op.drop_column("dns_record", "auto_tls_probe")
    op.drop_column("dns_zone", "auto_tls_probe")
