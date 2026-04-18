"""Align ``subnet.block_id`` with the model: enforce NOT NULL.

Historical schema drift — the SQLAlchemy model declared ``nullable=False``
but no migration ever issued the matching ``ALTER COLUMN SET NOT NULL`` on
the table. That allowed stray rows with ``block_id IS NULL`` to creep in
whenever a block was deleted before the ``ondelete=RESTRICT`` guard was
in place (or through direct SQL). Those rows then exploded the
``GET /subnets`` response-model validation.

This migration:

* Defensively pins any straggler subnet to its space's first block — we
  do not want the upgrade to fail at ``SET NOT NULL`` time and we do not
  want to silently drop data. The assignment is arbitrary but keeps the
  row visible under *something* so the operator can reorg it from the UI.
* Flips the column to ``NOT NULL``.

Downgrade just drops the ``NOT NULL`` constraint.

Revision ID: e5b831f02db9
Revises: a92f317b5d08
Create Date: 2026-04-18 02:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5b831f02db9"
down_revision: str | None = "a92f317b5d08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rescue any orphan rows left over from the drift era. For each subnet
    # whose ``block_id`` is NULL, try to reattach it to a block in its
    # space whose CIDR contains the subnet's network. Fall back to the
    # first block in the space. If the space has no blocks at all, the
    # operator will have to clean up by hand — the ALTER below will then
    # raise, which is the intended loud failure.
    op.execute("""
        UPDATE subnet AS s
        SET block_id = (
            SELECT b.id FROM ip_block b
            WHERE b.space_id = s.space_id
              AND b.network >>= s.network
            ORDER BY masklen(b.network) DESC
            LIMIT 1
        )
        WHERE s.block_id IS NULL
          AND EXISTS (
            SELECT 1 FROM ip_block b
            WHERE b.space_id = s.space_id
              AND b.network >>= s.network
          );
        """)
    op.execute("""
        UPDATE subnet AS s
        SET block_id = (
            SELECT b.id FROM ip_block b
            WHERE b.space_id = s.space_id
            ORDER BY b.network
            LIMIT 1
        )
        WHERE s.block_id IS NULL;
        """)

    op.alter_column(
        "subnet", "block_id", existing_type=sa.dialects.postgresql.UUID(), nullable=False
    )

    # Align the FK with the model: the model declares ``ondelete=RESTRICT``
    # but the live table has ``ON DELETE SET NULL``, which is what allowed
    # the orphan rows in the first place. Drop the drifted constraint and
    # recreate it with RESTRICT. ``delete_block`` now also pre-checks for
    # this case and returns 409, but the DB is the last line of defence.
    op.drop_constraint("subnet_block_id_fkey", "subnet", type_="foreignkey")
    op.create_foreign_key(
        "subnet_block_id_fkey",
        "subnet",
        "ip_block",
        ["block_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("subnet_block_id_fkey", "subnet", type_="foreignkey")
    op.create_foreign_key(
        "subnet_block_id_fkey",
        "subnet",
        "ip_block",
        ["block_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(
        "subnet", "block_id", existing_type=sa.dialects.postgresql.UUID(), nullable=True
    )
