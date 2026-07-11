"""DHCP scope soft-delete cascades to its pools + reservations (#617), and
repairs the IPAM mirrors an earlier hard-delete stranded (#618).

Revision ID: b3e7d21c9f04
Revises: a7c3e91f4d28
Create Date: 2026-07-11 12:00:00

``DHCPScope`` has been soft-deletable since ``c1f4a8b27d09``, but the cascade
walk treated it as a leaf. Its pools and reservations were therefore left as
live, un-stamped rows pointing at a hidden parent — invisible in the UI, but
still answering ``GET /scopes/{id}/statics`` and still enforcing group-wide MAC
uniqueness against a scope the operator could no longer see.

This migration gives ``dhcp_pool`` and ``dhcp_static_assignment`` the same three
``SoftDeleteMixin`` columns every other cascade child carries, so they can ride
their scope's ``deletion_batch_id`` into the trash and come back with it.

Three things beyond the plain column adds:

1. The two reservation uniqueness rules become **partial** unique indexes
   (``WHERE deleted_at IS NULL``), same shape as ``uq_dhcp_scope_group_subnet``
   (#474). Otherwise a trashed reservation would hold the (scope, mac) /
   (scope, ip) slot against a live one.

2. **Backfill.** Any pool / reservation whose scope is *already* soft-deleted is
   stamped with that scope's ``deleted_at`` / ``deleted_by_user_id`` /
   ``deletion_batch_id``. Without this, rows trashed before this release would
   stay live-but-orphaned forever — and a restore of their scope would bring the
   scope back while they carried no batch to be restored *with*.

3. **IPAM mirror repair** (#618). Releases ``ip_address`` rows stranded at
   ``status='static_dhcp'`` with a ``static_assignment_id`` that no longer
   resolves — the residue of scope hard-deletes/purges that removed the
   reservation via FK CASCADE without running the Python detach. They are
   returned to ``available`` (per the #478 rule: ``allocated`` would shadow a
   future dynamic lease at that IP and never be reaped).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b3e7d21c9f04"
down_revision: str | None = "a7c3e91f4d28"
branch_labels = None
depends_on = None


_CASCADE_CHILD_TABLES = ("dhcp_pool", "dhcp_static_assignment")


def upgrade() -> None:
    # ── 1. SoftDeleteMixin columns on the two cascade children ──────────────
    for table in _CASCADE_CHILD_TABLES:
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
        op.create_index(f"ix_{table}_deleted_at", table, ["deleted_at"])
        op.create_index(f"ix_{table}_deletion_batch_id", table, ["deletion_batch_id"])

    # ── 2. Reservation uniqueness becomes partial (live rows only) ──────────
    op.drop_constraint("uq_dhcp_static_scope_mac", "dhcp_static_assignment", type_="unique")
    op.drop_constraint("uq_dhcp_static_scope_ip", "dhcp_static_assignment", type_="unique")
    op.create_index(
        "uq_dhcp_static_scope_mac",
        "dhcp_static_assignment",
        ["scope_id", "mac_address"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_dhcp_static_scope_ip",
        "dhcp_static_assignment",
        ["scope_id", "ip_address"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ── 3. Backfill: adopt children of already-trashed scopes into the batch ─
    for table in _CASCADE_CHILD_TABLES:
        op.execute(sa.text(f"""
                UPDATE {table} AS c
                SET deleted_at = s.deleted_at,
                    deleted_by_user_id = s.deleted_by_user_id,
                    deletion_batch_id = s.deletion_batch_id
                FROM dhcp_scope AS s
                WHERE c.scope_id = s.id
                  AND s.deleted_at IS NOT NULL
                  AND c.deleted_at IS NULL
                """))

    # ── 4. #618 repair: release IPAM mirrors of reservations that no longer exist
    # Only rows whose pointer is a well-formed UUID resolving to nothing are
    # touched; a malformed pointer is left alone rather than guessed at. The
    # regex guard keeps the ``::uuid`` cast from erroring on legacy junk — it is
    # evaluated before the cast because Postgres short-circuits AND left to
    # right here (both are strict, index-free predicates on the same row).
    uuid_re = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    op.execute(sa.text("""
            UPDATE ip_address
            SET status = 'available',
                static_assignment_id = NULL
            WHERE id IN (
                SELECT a.id FROM ip_address a
                WHERE a.status = 'static_dhcp'
                  AND a.static_assignment_id IS NOT NULL
                  AND a.static_assignment_id ~ :uuid_re
                  AND NOT EXISTS (
                      SELECT 1 FROM dhcp_static_assignment d
                      WHERE d.id = a.static_assignment_id::uuid
                  )
            )
            """).bindparams(uuid_re=uuid_re))


def downgrade() -> None:
    # Restore the plain (non-partial) unique constraints. Any duplicate that a
    # partial index allowed (a trashed reservation sharing a live one's
    # (scope, mac)) would block this — purge the trash first.
    op.drop_index("uq_dhcp_static_scope_ip", table_name="dhcp_static_assignment")
    op.drop_index("uq_dhcp_static_scope_mac", table_name="dhcp_static_assignment")
    op.create_unique_constraint(
        "uq_dhcp_static_scope_mac", "dhcp_static_assignment", ["scope_id", "mac_address"]
    )
    op.create_unique_constraint(
        "uq_dhcp_static_scope_ip", "dhcp_static_assignment", ["scope_id", "ip_address"]
    )

    for table in reversed(_CASCADE_CHILD_TABLES):
        op.drop_index(f"ix_{table}_deletion_batch_id", table_name=table)
        op.drop_index(f"ix_{table}_deleted_at", table_name=table)
        op.drop_column(table, "deletion_batch_id")
        op.drop_column(table, "deleted_by_user_id")
        op.drop_column(table, "deleted_at")
