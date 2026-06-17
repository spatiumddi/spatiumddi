"""saved_view table + ui.saved_views feature module (#77)

Per-user named filter/sort/column state for list pages (issue #77).
One global table; rows are owned by the creating user (CASCADE) and
never shared. Plus the ``ui.saved_views`` feature-module seed
(default-enabled) so the ``/api/v1/saved-views`` surface gates behind
one toggle (non-negotiable #14).

Revision ID: d4e9f2a7c1b8
Revises: c3d8a1f9e62b
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4e9f2a7c1b8"
down_revision: str | None = "c3d8a1f9e62b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "saved_view",
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
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "page", "name", name="uq_saved_view_user_page_name"),
    )
    op.create_index(
        "ix_saved_view_user_page",
        "saved_view",
        ["user_id", "page"],
    )

    # ── feature_module seed (non-negotiable #14) ────────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('ui.saved_views', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'ui.saved_views'"))
    op.drop_index("ix_saved_view_user_page", table_name="saved_view")
    op.drop_table("saved_view")
