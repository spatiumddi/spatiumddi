"""embedded ACME client — accounts + orders + LE Web-UI cert settings (#438)

Phase 1 of the embedded ACME *client* (issue #438): SpatiumDDI acting
as an ACME client against a public CA (Let's Encrypt) to issue a
CA-trusted Web UI TLS cert, solving the DNS-01 challenge through its
own managed DNS zones. Adds:

* ``acme_client_account`` — the operator's ACME account at the CA
  (directory URL + CA-assigned account URL + Fernet-encrypted account
  key, optional EAB).
* ``acme_order`` — one row per issuance attempt; ON success its
  ``certificate_id`` FK points at the issued ``appliance_certificate``
  row (source="letsencrypt").
* five ``platform_settings`` columns gating + pre-filling issuance.
* the ``security.certificates`` feature-module seed (default-enabled —
  discovery toggle; issuance is separately RBAC + acme_enabled gated,
  non-negotiable #14).

Revision ID: a7f2c9e4d1b8
Revises: c3a1f9d24b80
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7f2c9e4d1b8"
down_revision: str | None = "c3a1f9d24b80"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "acme_client_account",
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
        sa.Column("directory_url", sa.Text(), nullable=False),
        sa.Column("account_url", sa.Text(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("account_key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("eab_kid", sa.String(length=255), nullable=True),
        sa.Column("eab_hmac_encrypted", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "acme_order",
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
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("domains", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "challenge_type",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'dns-01'"),
        ),
        sa.Column("dns_provider", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("order_url", sa.Text(), nullable=True),
        sa.Column("finalize_url", sa.Text(), nullable=True),
        sa.Column("certificate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["acme_client_account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["certificate_id"], ["appliance_certificate.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_acme_order_account_id", "acme_order", ["account_id"])

    # ── platform_settings columns (issue #438) ──────────────────────────
    op.add_column(
        "platform_settings",
        sa.Column("acme_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "platform_settings",
        sa.Column("acme_auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "acme_challenge_type",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'dns-01'"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column("acme_dns_provider", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "acme_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # ── feature_module seed (non-negotiable #14) ────────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('security.certificates', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'security.certificates'"))
    op.drop_column("platform_settings", "acme_domains")
    op.drop_column("platform_settings", "acme_dns_provider")
    op.drop_column("platform_settings", "acme_challenge_type")
    op.drop_column("platform_settings", "acme_auto_renew")
    op.drop_column("platform_settings", "acme_enabled")
    op.drop_index("ix_acme_order_account_id", table_name="acme_order")
    op.drop_table("acme_order")
    op.drop_table("acme_client_account")
