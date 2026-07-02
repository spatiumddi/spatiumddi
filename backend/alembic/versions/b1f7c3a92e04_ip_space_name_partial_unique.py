"""ip_space.name uniqueness: partial index excluding soft-deleted rows (#491)

``ix_ip_space_name`` was a plain unique index (``name`` column had
``unique=True``), but ``IPSpace`` is soft-deletable — so a trashed space
kept occupying its name slot, and re-creating a space with that name (the
ORM create pre-check is auto-filtered to ``deleted_at IS NULL`` and so
can't see the trashed row) raised a raw ``IntegrityError`` → HTTP 500, with
the name unusable until the 30-day purge.

Fix: make the uniqueness a **partial** unique index (``WHERE deleted_at IS
NULL``) so only live spaces are unique by name; trashed rows fall out of
the index. Active rows were already globally unique under the old index, so
no dedup is needed before creating the new one. Same pattern as
``uq_dhcp_scope_group_subnet`` (a3f9c1e7b2d4).

Revision ID: b1f7c3a92e04
Revises: a3f9c1e7b2d4
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b1f7c3a92e04"
down_revision = "a3f9c1e7b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_ip_space_name", table_name="ip_space")
    op.create_index(
        "ix_ip_space_name",
        "ip_space",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ip_space_name", table_name="ip_space")
    op.create_index("ix_ip_space_name", "ip_space", ["name"], unique=True)
