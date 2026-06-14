"""#424 — backfill SRV/MX structured fields that the old UI left NULL

Before #424 the Add/Edit Record form couldn't set an SRV's weight/port (and
could leave an MX priority NULL), so existing rows carry NULLs that every
driver silently renders as 0 (SRV) / 10 (MX). #424 now *requires* those
fields on create and validates the merged row on update — which would make a
pre-existing NULL-weight SRV un-editable (any edit 422s "SRV records require
weight, port"). This one-shot data migration backfills those rows to the
exact values the drivers already substitute, so:

  * the rendered wire output is unchanged (bind9/powerdns/windows all map a
    NULL SRV field → 0 and a NULL MX priority → 10 today), and
  * post-upgrade edits validate instead of 422-ing.

Data-only; the downgrade is a no-op (a backfilled 0/10 is indistinguishable
from an operator-set 0/10, so there is nothing safe to revert).

Revision ID: f4a1c8e92b07
Revises: c5f1a2b3d4e6
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op

revision = "f4a1c8e92b07"
down_revision = "c5f1a2b3d4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SRV uses priority + weight + port; a NULL renders as 0.
    op.execute("""
        UPDATE dns_record
           SET priority = COALESCE(priority, 0),
               weight   = COALESCE(weight, 0),
               port     = COALESCE(port, 0)
         WHERE record_type = 'SRV'
           AND (priority IS NULL OR weight IS NULL OR port IS NULL)
        """)
    # MX uses priority (the preference); a NULL renders as 10.
    op.execute("""
        UPDATE dns_record
           SET priority = 10
         WHERE record_type = 'MX'
           AND priority IS NULL
        """)


def downgrade() -> None:
    # No-op: a backfilled 0/10 can't be told apart from an operator-set
    # value, so there is nothing safe to undo.
    pass
