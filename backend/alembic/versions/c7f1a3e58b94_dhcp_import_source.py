"""dhcp_import: import_source + imported_at on dhcp scope/pool/static/class + dhcp.import module

Revision ID: c7f1a3e58b94
Revises: f2b6d4a91c37
Create Date: 2026-05-30 12:00:00.000000

Provenance columns the DHCP configuration importer (issue #129) stamps
on every row it creates, plus the ``dhcp.import`` feature-module seed so
the importer surface gates behind a single toggle (mirrors the DNS
importer's ``b7e2d9a5f314`` migration).

* ``import_source`` — short text, NULL for hand-created rows. Values
  ``kea | windows_dhcp | isc_dhcp`` map 1:1 to the three sources the
  importer speaks. Indexed (partial, ``IS NOT NULL``) so "show me
  everything I imported from last Tuesday's ISC cutover" stays an
  index scan.
* ``imported_at`` — wall-clock timestamp of the commit. Lets a re-import
  of the same source recognise rows a previous run created.

Both columns are nullable + non-default — pre-existing rows stay
untouched and look "hand-created" to the importer's match logic, which
is correct: the importer must never claim ownership of rows it didn't
create. Carried on the four config-bearing DHCP tables the importer
writes (scope / pool / static assignment / client class); server +
group rows are not provenance-stamped (the importer reuses existing
groups, it doesn't mint servers).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7f1a3e58b94"
down_revision: Union[str, None] = "f2b6d4a91c37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = (
    "dhcp_scope",
    "dhcp_pool",
    "dhcp_static_assignment",
    "dhcp_client_class",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("import_source", sa.String(length=20), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            f"ix_{table}_import_source",
            table,
            ["import_source"],
            postgresql_where=sa.text("import_source IS NOT NULL"),
        )

    # ── feature_module seed ────────────────────────────────────────────
    op.execute(
        sa.text(
            """
            INSERT INTO feature_module (id, enabled)
            VALUES ('dhcp.import', TRUE)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'dhcp.import'"))
    for table in _TABLES:
        op.drop_index(f"ix_{table}_import_source", table_name=table)
        op.drop_column(table, "imported_at")
        op.drop_column(table, "import_source")
