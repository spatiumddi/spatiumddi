"""netbox_import: import_source + imported_at on IPAM tables + ipam.import.netbox module

Revision ID: 30135c361a47
Revises: a3f1d6c92b58
Create Date: 2026-06-26 12:00:00.000000

Provenance columns the NetBox read-only one-shot importer (issue #36)
stamps on every row it creates, plus the ``ipam.import.netbox``
feature-module seed so the importer surface gates behind a single
toggle (mirrors the DNS importer's ``b7e2d9a5f314`` and the DHCP
importer's ``c7f1a3e58b94`` migrations).

* ``import_source`` — short text, NULL for hand-created rows. The
  single value ``netbox`` maps 1:1 to the one source this importer
  speaks. Indexed (partial, ``IS NOT NULL``) so "show me everything I
  imported from last week's NetBox cutover" stays an index scan.
* ``imported_at`` — wall-clock timestamp of the commit. Lets a
  re-import recognise rows a previous run created.

Both columns are nullable + non-default — pre-existing rows stay
untouched and look "hand-created" to the importer's match logic, which
is correct: the importer must never claim ownership of rows it didn't
create. Carried on the eight IPAM/ownership/network tables the importer
writes (space / block / subnet / address / VRF / VLAN / customer /
site). The synthetic import Router is matched by its UNIQUE ``name`` on
re-import, so it carries no provenance columns. VLAN has neither
``tags`` nor ``custom_fields``, so for VLANs these provenance columns
are the only NetBox marker available.

Also seeds the ``ipam.import.netbox`` feature module ``enabled=TRUE``
to match the catalog default (importers are default-on for
discoverability per non-negotiable #13/#14; the endpoints are
RBAC-gated + superadmin separately).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "30135c361a47"
down_revision: str | None = "a3f1d6c92b58"
branch_labels: str | None = None
depends_on: str | None = None


# Tables the NetBox importer writes — each gets the two provenance
# columns + a partial index on ``import_source``.
_TABLES = (
    "ip_space",
    "ip_block",
    "subnet",
    "ip_address",
    "vrf",
    "vlan",
    "customer",
    "site",
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
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('ipam.import.netbox', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'ipam.import.netbox'"))
    for table in _TABLES:
        op.drop_index(f"ix_{table}_import_source", table_name=table)
        op.drop_column(table, "imported_at")
        op.drop_column(table, "import_source")
