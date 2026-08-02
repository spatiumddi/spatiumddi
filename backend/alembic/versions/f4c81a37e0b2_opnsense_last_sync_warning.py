"""Add opnsense_router.last_sync_warning (#797)

A reconcile pass can succeed end-to-end and still mirror nothing
useful. ``ReconcileSummary.warnings`` already recorded why — no DHCP
backend detected, a subnet owned by another integration, an address we
declined to claim — but nothing persisted it, so the only operator-
visible signal was a green "ok" with zero counts. This column is where
the joined warnings land, surfaced as an amber state in the UI
alongside the existing ``last_sync_error`` red one.

Revision ID: f4c81a37e0b2
Revises: e3b7a1c25d94
Create Date: 2026-08-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c81a37e0b2"
down_revision: str | None = "e3b7a1c25d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opnsense_router",
        sa.Column("last_sync_warning", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opnsense_router", "last_sync_warning")
