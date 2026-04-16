"""add auth_provider + auth_group_mapping tables; add app_base_url to platform_settings

Revision ID: c4d9a1e6f827
Revises: a3f7b2c8d419
Create Date: 2026-04-16 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4d9a1e6f827"
down_revision: str | None = "a3f7b2c8d419"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column("app_base_url", sa.String(length=500), nullable=False, server_default=""),
    )

    op.create_table(
        "auth_provider",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("secrets_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("auto_create_users", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_update_users", sa.Boolean(), nullable=False, server_default=sa.true()),
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
    )

    op.create_table(
        "auth_group_mapping",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("auth_provider.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_group", sa.String(length=1000), nullable=False),
        sa.Column(
            "internal_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
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
        sa.UniqueConstraint(
            "provider_id", "external_group", name="uq_auth_group_mapping_external"
        ),
    )
    op.create_index(
        "ix_auth_group_mapping_provider_id", "auth_group_mapping", ["provider_id"]
    )
    op.create_index(
        "ix_auth_group_mapping_internal_group_id",
        "auth_group_mapping",
        ["internal_group_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_group_mapping_internal_group_id", table_name="auth_group_mapping")
    op.drop_index("ix_auth_group_mapping_provider_id", table_name="auth_group_mapping")
    op.drop_table("auth_group_mapping")
    op.drop_table("auth_provider")
    op.drop_column("platform_settings", "app_base_url")
