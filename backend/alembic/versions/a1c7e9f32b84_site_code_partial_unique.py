"""site code uniqueness: partial index on non-NULL code (#279 bug)

The ``ix_site_parent_code_unique`` index was created over
``(parent_site_id, code)`` with ``NULLS NOT DISTINCT`` so two top-level
sites (NULL parent) can't share a code. But that flag also makes the
optional ``code`` column's NULLs (and empty strings) compare equal, so a
second code-less sibling — including the common case of a second
top-level site created with no code — 409'd with "a sibling site with
this code already exists" despite having no code.

Fix: make the unique index partial (``WHERE code IS NOT NULL``) so
code-less rows are excluded from it entirely; real codes stay unique per
parent with NULL parents sharing one namespace. Existing empty/whitespace
codes are normalised to NULL first (create/update now do the same).

Revision ID: a1c7e9f32b84
Revises: f3b8d24a1c70
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a1c7e9f32b84"
down_revision = "f3b8d24a1c70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_site_parent_code_unique", table_name="site")
    # Normalise existing empty/whitespace codes to NULL so they fall out
    # of the partial index (and match the API's new "" → NULL coercion).
    op.execute("UPDATE site SET code = NULL WHERE btrim(code) = ''")
    op.create_index(
        "ix_site_parent_code_unique",
        "site",
        ["parent_site_id", "code"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_site_parent_code_unique", table_name="site")
    op.create_index(
        "ix_site_parent_code_unique",
        "site",
        ["parent_site_id", "code"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
