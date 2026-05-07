"""dns_zone DNSSEC DS-record cache columns (issue #127, Phase 3c.fe)

Revision ID: e7f94b21c8d5
Revises: d2a8e417b9f3
Create Date: 2026-05-07 22:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e7f94b21c8d5"
down_revision = "d2a8e417b9f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Two columns the agent populates after a successful PowerDNS sign:

    * ``dnssec_ds_records`` — JSONB array of DS rrset strings the operator
      copies into their parent registrar (e.g. ``["12345 13 2 abc..."]``).
    * ``dnssec_synced_at`` — UTC timestamp of the agent's most recent
      DNSSEC state push for this zone. Lets the UI distinguish
      "signed but never reported" from "signed and freshly synced".

    Both nullable since the vast majority of zones never sign.
    """
    op.add_column(
        "dns_zone",
        sa.Column(
            "dnssec_ds_records",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "dns_zone",
        sa.Column(
            "dnssec_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_zone", "dnssec_synced_at")
    op.drop_column("dns_zone", "dnssec_ds_records")
