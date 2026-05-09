"""dns_import: import_source + imported_at on dns_zone/dns_record + dns.import feature module

Revision ID: b7e2d9a5f314
Revises: a1f4d97c8e25
Create Date: 2026-05-09 12:00:00.000000

Adds the provenance columns the DNS configuration importer (issue #128)
stamps on every row it creates, and seeds the ``dns.import`` feature
module so the importer surface gates behind a single toggle.

* ``import_source`` — short text, NULL for hand-created rows. Values
  ``bind9 | windows_dns | powerdns`` map 1:1 to the three sources the
  importer speaks. Indexed so "show me everything I imported from the
  big BIND9 cutover last Tuesday" stays a single index scan.
* ``imported_at`` — wall-clock timestamp of the commit. Lets re-imports
  of the same source decide "this row already came from a previous
  run, skip" by matching on ``(import_source, fqdn, record_type, value)``.

Both columns are nullable + non-default — pre-existing rows stay
untouched and look like "hand-created" to the importer's match logic,
which is the correct behaviour: the importer should never claim
ownership of records it didn't create.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "b7e2d9a5f314"
down_revision = "a1f4d97c8e25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── DNSZone ────────────────────────────────────────────────────────
    op.add_column(
        "dns_zone",
        sa.Column("import_source", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "dns_zone",
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_dns_zone_import_source",
        "dns_zone",
        ["import_source"],
        postgresql_where=sa.text("import_source IS NOT NULL"),
    )

    # ── DNSRecord ──────────────────────────────────────────────────────
    op.add_column(
        "dns_record",
        sa.Column("import_source", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "dns_record",
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_dns_record_import_source",
        "dns_record",
        ["import_source"],
        postgresql_where=sa.text("import_source IS NOT NULL"),
    )

    # ── feature_module seed ────────────────────────────────────────────
    op.execute(
        sa.text(
            """
            INSERT INTO feature_module (id, enabled)
            VALUES ('dns.import', TRUE)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'dns.import'"))
    op.drop_index("ix_dns_record_import_source", table_name="dns_record")
    op.drop_column("dns_record", "imported_at")
    op.drop_column("dns_record", "import_source")
    op.drop_index("ix_dns_zone_import_source", table_name="dns_zone")
    op.drop_column("dns_zone", "imported_at")
    op.drop_column("dns_zone", "import_source")
