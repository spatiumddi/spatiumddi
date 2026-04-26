"""Soft-delete + 30-day recovery for IPAM / DNS / DHCP high-blast-radius rows.

Revision ID: c1f4a8b27d09
Revises: e6f12b9a3c84
Create Date: 2026-04-26 15:00:00

Adds three columns to each in-scope table — ``deleted_at`` (TIMESTAMPTZ,
indexed), ``deleted_by_user_id`` (UUID FK to ``user.id``, ON DELETE SET
NULL), and ``deletion_batch_id`` (UUID, indexed). Cascading deletes share
a ``deletion_batch_id`` so a single restore brings them all back
atomically; standalone deletes still get a fresh batch UUID for the same
restore-by-batch lookup.

Also adds ``platform_settings.soft_delete_purge_days`` (INTEGER, default
30) — the nightly purge sweep gate. Set to 0 to disable purge entirely.

Tables affected:
  * ``ip_space``
  * ``ip_block``
  * ``subnet``
  * ``dns_zone``
  * ``dns_record``
  * ``dhcp_scope``

The DB-side server defaults are NULL (i.e. existing rows remain "live"
post-migration). The fields are operator-driven via the API delete
handlers; nothing on the DB side ever stamps them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c1f4a8b27d09"
down_revision: str | None = "f1c8b2a945d3"
branch_labels = None
depends_on = None


_SOFT_DELETE_TABLES = (
    "ip_space",
    "ip_block",
    "subnet",
    "dns_zone",
    "dns_record",
    "dhcp_scope",
)


def upgrade() -> None:
    for table in _SOFT_DELETE_TABLES:
        op.add_column(
            table,
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            table,
            sa.Column(
                "deleted_by_user_id",
                sa.UUID(),
                sa.ForeignKey("user.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.add_column(
            table,
            sa.Column("deletion_batch_id", sa.UUID(), nullable=True),
        )
        op.create_index(
            f"ix_{table}_deleted_at",
            table,
            ["deleted_at"],
        )
        op.create_index(
            f"ix_{table}_deletion_batch_id",
            table,
            ["deletion_batch_id"],
        )

    op.add_column(
        "platform_settings",
        sa.Column(
            "soft_delete_purge_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "soft_delete_purge_days")

    for table in reversed(_SOFT_DELETE_TABLES):
        op.drop_index(f"ix_{table}_deletion_batch_id", table_name=table)
        op.drop_index(f"ix_{table}_deleted_at", table_name=table)
        op.drop_column(table, "deletion_batch_id")
        op.drop_column(table, "deleted_by_user_id")
        op.drop_column(table, "deleted_at")
