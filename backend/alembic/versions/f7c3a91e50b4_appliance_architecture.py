"""Architecture on the appliance + upgrade image (#1026).

Two nullable columns, no backfill, no data migration.

The gate they feed refuses to hand a node an upgrade image built for
another architecture. Both are NULLABLE and stay NULL for every existing
row on purpose: NULL is UNKNOWN, and UNKNOWN is not a conflict.

A backfill of ``'amd64'`` would be *true today* — every appliance and
every published slot image is x86-64, because no other build exists —
and would still be the wrong thing to write. It would assert as fact
something the row never reported, so the first arm64 image uploaded by
an operator who does not set the field would inherit an amd64 claim from
this migration and be handed to an amd64 node with the gate reporting a
match. An honest UNKNOWN falls through to the host runner's own check on
the real bytes; a confident wrong answer does not.

Revision ID: f7c3a91e50b4
Revises: b8f4c02e7a19
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7c3a91e50b4"
down_revision = "b8f4c02e7a19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appliance", sa.Column("architecture", sa.String(length=16), nullable=True))
    op.add_column(
        "appliance_upgrade_image",
        sa.Column("architecture", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance_upgrade_image", "architecture")
    op.drop_column("appliance", "architecture")
