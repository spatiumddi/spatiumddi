"""appliance_certificate (Phase 4b.1)

Storage for the SpatiumDDI OS appliance Web UI certificate. Operators
upload PEM cert + key via /api/v1/appliance/tls; nginx serves whichever
row carries is_active=true. Private key is Fernet-encrypted at rest.

Revision ID: c9f2a83b04d7
Revises: b7e2d9a5f314
Create Date: 2026-05-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "c9f2a83b04d7"
down_revision: str | None = "b7e2d9a5f314"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "appliance_certificate",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("cert_pem", sa.Text(), nullable=False),
        sa.Column("key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subject_cn", sa.String(length=255), nullable=False),
        sa.Column(
            "sans_json",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("issuer_cn", sa.String(length=255), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=95), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("csr_pem", sa.Text(), nullable=True),
        sa.Column("csr_subject", JSONB(), nullable=True),
        sa.UniqueConstraint("name", name="uq_appliance_certificate_name"),
    )
    # Most lookups are either "the active one" (1-row probe) or "list
    # all". A small partial index on is_active=true gives the active
    # lookup an index seek instead of a sequential scan; the table
    # stays tiny enough that the second case is fine table-scanned.
    op.create_index(
        "ix_appliance_certificate_active",
        "appliance_certificate",
        ["is_active"],
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_appliance_certificate_active",
        table_name="appliance_certificate",
    )
    op.drop_table("appliance_certificate")
