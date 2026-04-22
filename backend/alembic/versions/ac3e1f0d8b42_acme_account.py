"""ACME (DNS-01 provider) account table.

Revision ID: ac3e1f0d8b42
Revises: e4b9f07d25a1
Create Date: 2026-04-21 22:30:00

Single new table ``acme_account`` holding acme-dns-compatible
credentials. Each row is scoped to one DNSZone + one unique
subdomain label — the standard acme-dns delegation pattern limits
blast radius of a leaked credential to exactly that subdomain.

No data backfill: the ACME provider surface is opt-in; existing
deployments have no credentials to migrate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ac3e1f0d8b42"
down_revision: str | None = "e4b9f07d25a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acme_account",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username", sa.String(length=64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("subdomain", sa.String(length=64), nullable=False, unique=True),
        sa.Column("zone_id", sa.UUID(), nullable=False),
        sa.Column(
            "allowed_source_cidrs", postgresql.JSONB(), nullable=True
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["dns_zone.id"],
            ondelete="CASCADE",
            name="fk_acme_account_zone",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_acme_account_created_by",
        ),
    )
    op.create_index(
        "ix_acme_account_username",
        "acme_account",
        ["username"],
        unique=True,
    )
    op.create_index(
        "ix_acme_account_subdomain",
        "acme_account",
        ["subdomain"],
        unique=True,
    )
    op.create_index(
        "ix_acme_account_zone",
        "acme_account",
        ["zone_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_acme_account_zone", table_name="acme_account")
    op.drop_index("ix_acme_account_subdomain", table_name="acme_account")
    op.drop_index("ix_acme_account_username", table_name="acme_account")
    op.drop_table("acme_account")
