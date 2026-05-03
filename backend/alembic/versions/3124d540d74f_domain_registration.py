"""domain registration tracking — RDAP/WHOIS, NS drift, expiry

Revision ID: 3124d540d74f
Revises: 2c4e9d1a7f63
Create Date: 2026-05-02 00:00:00.000000

Phase 1 of issue #87 — distinct from ``dns_zone``. Tracks the
registration side of a name: registrar, registrant, expiry, and the
nameservers the registry advertises versus what the operator expects.
RDAP-driven; WHOIS-fallback + the scheduled refresh task land in
follow-ups.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3124d540d74f"
down_revision: Union[str, None] = "2c4e9d1a7f63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "domain",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("registrar", sa.String(length=255), nullable=True),
        sa.Column("registrant_org", sa.String(length=255), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "expected_nameservers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "actual_nameservers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "nameserver_drift",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "dnssec_signed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("whois_last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "whois_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "whois_state",
            sa.String(length=16),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_domain_name", "domain", ["name"], unique=True)
    op.create_index("ix_domain_whois_state", "domain", ["whois_state"], unique=False)
    op.create_index("ix_domain_expires_at", "domain", ["expires_at"], unique=False)
    op.create_index("ix_domain_next_check_at", "domain", ["next_check_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_domain_next_check_at", table_name="domain")
    op.drop_index("ix_domain_expires_at", table_name="domain")
    op.drop_index("ix_domain_whois_state", table_name="domain")
    op.drop_index("ix_domain_name", table_name="domain")
    op.drop_table("domain")
