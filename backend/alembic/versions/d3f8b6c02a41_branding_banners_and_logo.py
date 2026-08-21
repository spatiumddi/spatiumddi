"""Branding — login banner, environment banner, and operator logo storage.

Revision ID: d3f8b6c02a41
Revises: f4c81a37e0b2
Create Date: 2026-08-21 00:00:00

Issues #885 (login acceptable-use banner), #886 (custom logo), #887
(environment banner). All new ``platform_settings`` columns are NOT NULL
with a server_default so the existing singleton row backfills in place,
and every default reproduces today's behaviour — an install that upgrades
into this migration renders exactly as it did before, with both banners
off and no custom logo.

The logo lives in its own table rather than as a column on
``platform_settings``: that row is read on many request paths, and none of
them should pay for a blob. Storing the bytes in Postgres (rather than on
a volume) is what makes a custom logo appear on every API pod without a
shared filesystem — the same problem the slot-image mirror sidecar (#296)
had to solve the hard way.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d3f8b6c02a41"
down_revision: str | None = "f4c81a37e0b2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Login-screen acceptable-use banner (#885) ──────────────────────
    op.add_column(
        "platform_settings",
        sa.Column(
            "login_banner_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "login_banner_title",
            sa.String(length=120),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "login_banner_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "login_banner_require_ack",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── Environment banner (#887) ──────────────────────────────────────
    op.add_column(
        "platform_settings",
        sa.Column(
            "env_banner_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "env_banner_text",
            sa.String(length=200),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "env_banner_bg",
            sa.String(length=7),
            nullable=False,
            server_default=sa.text("'#b91c1c'"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "env_banner_fg",
            sa.String(length=7),
            nullable=False,
            server_default=sa.text("'#ffffff'"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "env_banner_position",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'top'"),
        ),
    )

    # ── Operator-uploaded branding assets (#886) ───────────────────────
    op.create_table(
        "branding_asset",
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
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", name="uq_branding_asset_kind"),
    )
    op.create_index("ix_branding_asset_kind", "branding_asset", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_branding_asset_kind", table_name="branding_asset")
    op.drop_table("branding_asset")
    op.drop_column("platform_settings", "env_banner_position")
    op.drop_column("platform_settings", "env_banner_fg")
    op.drop_column("platform_settings", "env_banner_bg")
    op.drop_column("platform_settings", "env_banner_text")
    op.drop_column("platform_settings", "env_banner_enabled")
    op.drop_column("platform_settings", "login_banner_require_ack")
    op.drop_column("platform_settings", "login_banner_text")
    op.drop_column("platform_settings", "login_banner_title")
    op.drop_column("platform_settings", "login_banner_enabled")
