"""nmap_scan rename udp_top100 preset to udp_top1000.

Bumps the udp UDP-sweep preset from top-100 to top-1000 ports and
renames the preset name accordingly. Backfills existing rows so
historical scans round-trip cleanly through the API filters.
"""

from __future__ import annotations

from alembic import op


revision = "a8d6e10f3b59"
down_revision = "d6a39e84c512"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE nmap_scan SET preset = 'udp_top1000' WHERE preset = 'udp_top100'")


def downgrade() -> None:
    op.execute("UPDATE nmap_scan SET preset = 'udp_top100' WHERE preset = 'udp_top1000'")
