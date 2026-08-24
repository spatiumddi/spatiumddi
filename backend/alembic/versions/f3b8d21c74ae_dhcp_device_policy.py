"""dhcp device policy — fingerprint-driven client classes (#700)

Revision ID: f3b8d21c74ae
Revises: a2e7f31c9b48
Create Date: 2026-08-24

Joins the two halves of device profiling: fingerbank device classes on
one side, Kea client classes on the other. One row per policy; the
compiled match expression is derived at bundle-build time from the
observed ``dhcp_fingerprint`` rows rather than stored, so a newly
classified device joins its policy without an operator edit.

``class_name`` is stored rather than derived from ``name`` so a rename
cannot silently detach the pools that reference it via
``dhcp_pool.class_restriction``.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f3b8d21c74ae"
down_revision = "a2e7f31c9b48"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dhcp_device_policy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("class_name", sa.String(length=128), nullable=False),
        sa.Column(
            "device_classes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("lease_time", sa.Integer(), nullable=True),
        sa.Column("match_override", sa.Text(), nullable=True),
        sa.Column(
            "include_ambiguous",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_compiled_at", sa.DateTime(timezone=True), nullable=True),
        # TimestampMixin columns need an explicit server_default here: a
        # fresh install runs the migration, not create_all, and would
        # otherwise NULL-violate on the first insert.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("group_id", "name", name="uq_dhcp_device_policy_group_name"),
        sa.UniqueConstraint(
            "group_id", "class_name", name="uq_dhcp_device_policy_group_class_name"
        ),
    )
    op.create_index("ix_dhcp_device_policy_group", "dhcp_device_policy", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_dhcp_device_policy_group", table_name="dhcp_device_policy")
    op.drop_table("dhcp_device_policy")
