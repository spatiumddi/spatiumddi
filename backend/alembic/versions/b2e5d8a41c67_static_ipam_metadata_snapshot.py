"""dhcp_static_assignment.ipam_metadata_snapshot — lossless restore (#630)

Snapshots the operator-authored columns of a reservation's ``ip_address``
mirror (description / tags / custom_fields / owner / role / …) when the mirror
is hard-deleted on a wholesale reservation delete, so a Trash restore can
re-apply them instead of recreating a bare row. Nullable, no backfill.

Revision ID: b2e5d8a41c67
Revises: a1f4c7e92b30
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2e5d8a41c67"
down_revision = "a1f4c7e92b30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dhcp_static_assignment",
        sa.Column(
            "ipam_metadata_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_static_assignment", "ipam_metadata_snapshot")
