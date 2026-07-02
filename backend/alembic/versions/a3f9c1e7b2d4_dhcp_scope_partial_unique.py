"""dhcp_scope uniqueness: partial index excluding soft-deleted rows (#474)

``uq_dhcp_scope_group_subnet`` was a plain ``UNIQUE(group_id, subnet_id)``,
but ``DHCPScope`` is soft-deletable — so a trashed scope kept occupying the
``(group, subnet)`` slot, and re-creating a scope for that subnet (or the
Windows lease-import auto-create in ``pull_leases``) raised a raw
``IntegrityError`` → HTTP 500. Recover-in-place required purging the scope
from Trash.

Fix: make the uniqueness a **partial** unique index (``WHERE deleted_at IS
NULL``) so only live scopes are unique per ``(group, subnet)``; trashed rows
fall out of the index entirely. Active rows were already globally unique
under the old constraint, so no dedup is needed before creating the index.

Revision ID: a3f9c1e7b2d4
Revises: b7c2f1a9d4e6
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a3f9c1e7b2d4"
down_revision = "b7c2f1a9d4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_dhcp_scope_group_subnet", "dhcp_scope", type_="unique")
    op.create_index(
        "uq_dhcp_scope_group_subnet",
        "dhcp_scope",
        ["group_id", "subnet_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_dhcp_scope_group_subnet", table_name="dhcp_scope")
    op.create_unique_constraint(
        "uq_dhcp_scope_group_subnet", "dhcp_scope", ["group_id", "subnet_id"]
    )
