"""address_set table + ipam.address_sets feature module (#103)

Named, RBAC-scoped slices of a subnet's address space (issue #103). A
row carries its own ``resource_type="address_set"`` identity so edit of
a slice (e.g. ``.50``–``.99``) can be delegated without subnet-wide
write. Plus the ``ipam.address_sets`` feature-module seed
(default-enabled) so the ``/api/v1/address-sets`` surface gates behind
one toggle (non-negotiable #14).

Revision ID: e2b9c4f1a7d6
Revises: d8f3a1c6e09b
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2b9c4f1a7d6"
down_revision: str | None = "d8f3a1c6e09b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "address_set",
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
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "range_kind",
            sa.String(length=16),
            server_default="contiguous",
            nullable=False,
        ),
        sa.Column("start_address", postgresql.INET(), nullable=True),
        sa.Column("end_address", postgresql.INET(), nullable=True),
        sa.Column(
            "explicit_addresses",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(["subnet_id"], ["subnet.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subnet_id", "name", name="uq_address_set_subnet_name"),
        sa.CheckConstraint(
            "end_address IS NULL OR start_address <= end_address",
            name="ck_address_set_range_order",
        ),
    )
    op.create_index("ix_address_set_subnet_id", "address_set", ["subnet_id"])
    op.create_index("ix_address_set_customer_id", "address_set", ["customer_id"])
    op.create_index("ix_address_set_site_id", "address_set", ["site_id"])

    # ── feature_module seed (non-negotiable #14) ────────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('ipam.address_sets', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'ipam.address_sets'"))
    op.drop_index("ix_address_set_site_id", table_name="address_set")
    op.drop_index("ix_address_set_customer_id", table_name="address_set")
    op.drop_index("ix_address_set_subnet_id", table_name="address_set")
    op.drop_table("address_set")
