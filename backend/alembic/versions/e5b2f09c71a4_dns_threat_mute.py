"""dns threat: per-client mute (operator triage on findings)

Revision ID: e5b2f09c71a4
Revises: d1a7c34e9b60
Create Date: 2026-07-25 22:00:00.000000

Issue #699 follow-up — operators need a way to clear a finding they have
reviewed.

Modelled as a mute on the **client**, not a delete on the window rows:

* deleting wouldn't stick — the aggregator recomputes the current and
  previous hour every 15 minutes and upserts, and the backfill refills
  any bucket left empty, so a cleared recent finding would reappear
  within the quarter hour while an older one stayed gone;
* deleting destroys evidence of what may be an exfiltration event,
  where "we looked and it's the backup agent" is itself worth keeping.

Unique on ``client_ip`` so re-muting updates the existing decision
rather than stacking rows. ``muted_until`` NULL means indefinite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET, UUID

revision = "e5b2f09c71a4"
down_revision = "d1a7c34e9b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dns_threat_mute",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_ip", INET(), nullable=False),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "muted_by",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "muted_by_display",
            sa.String(length=120),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("client_ip", name="uq_dns_threat_mute_client"),
    )


def downgrade() -> None:
    op.drop_table("dns_threat_mute")
