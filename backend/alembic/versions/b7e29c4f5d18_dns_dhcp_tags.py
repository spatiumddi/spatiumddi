"""dns + dhcp tags columns (issue #104 phase 4)

Phase 1 of #104 wired ``?tag=`` filtering across every list endpoint
whose model already had a ``tags`` JSONB column. DNS + DHCP didn't —
this migration adds the missing columns so the same chip + filter
plumbing lights up there too.

Four tables get a ``tags JSONB NOT NULL DEFAULT '{}'``:

* ``dns_zone``
* ``dns_record``
* ``dhcp_scope``
* ``dhcp_static_assignment``

The ``server_default`` matters — existing rows backfill to ``{}``
on column add, which lets us declare ``NOT NULL`` in one step
without a separate UPDATE pass. The default also matches what
SQLAlchemy's ``default=dict`` emits at INSERT time, so the model
and the schema agree on "untagged is ``{}``, never NULL".

No GIN index added — the autocomplete + filter helpers use
``tags ? key`` and ``tags @> '{...}'`` which are already served by
Postgres' default ``jsonb_ops`` GIN class on the column. Adding an
explicit index would be premature optimisation; revisit if a future
operator deployment shows tag-filter latency on the DNS / DHCP
tables.

Revision ID: b7e29c4f5d18
Revises: e5a18c40729b
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "b7e29c4f5d18"
down_revision = "e5a18c40729b"
branch_labels = None
depends_on = None


_TABLES = ("dns_zone", "dns_record", "dhcp_scope", "dhcp_static_assignment")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "tags",
                JSONB,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "tags")
