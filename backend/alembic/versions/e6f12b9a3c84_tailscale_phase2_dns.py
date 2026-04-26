"""Tailscale Phase 2 — synthetic DNS zone + records provenance FK.

Revision ID: e6f12b9a3c84
Revises: d8c5f12a47b9
Create Date: 2026-04-26 12:00:00

Adds ``tailscale_tenant_id`` FK on ``dns_zone`` + ``dns_record``
(``ON DELETE CASCADE``) so the Phase 2 reconciler can materialise
``<tailnet>.ts.net`` zones and per-device A / AAAA records, with
the API blocking edits while the FK is non-null.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e6f12b9a3c84"
down_revision: str | None = "d8c5f12a47b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("dns_zone", "dns_record"):
        op.add_column(
            table,
            sa.Column(
                "tailscale_tenant_id",
                sa.UUID(),
                sa.ForeignKey("tailscale_tenant.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_tailscale_tenant_id",
            table,
            ["tailscale_tenant_id"],
        )


def downgrade() -> None:
    for table in ("dns_record", "dns_zone"):
        op.drop_index(f"ix_{table}_tailscale_tenant_id", table_name=table)
        op.drop_column(table, "tailscale_tenant_id")
