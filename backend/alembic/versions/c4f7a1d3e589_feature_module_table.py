"""feature_module table — operator-controlled visibility for whole
sidebar / REST / MCP surfaces.

The catalog of module ids lives in
``app.services.feature_modules.MODULES``. This migration creates the
table and seeds one row per catalog entry at its declared
``default_enabled`` so operators see the toggles materialised on first
boot. Future migrations seed additional rows as new modules ship; the
service layer also tolerates rows for unknown ids (forward-compat
during downgrades).

Revision ID: c4f7a1d3e589
Revises: b7e29c4f5d18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "c4f7a1d3e589"
down_revision = "b7e29c4f5d18"
branch_labels = None
depends_on = None


# Mirror of ``app.services.feature_modules.MODULES``. Kept inline so
# the migration stays self-contained — service-layer drift after the
# fact is fine (the service tolerates extra rows + missing rows).
_SEED_ROWS: tuple[tuple[str, bool], ...] = (
    ("network.customer", True),
    ("network.provider", True),
    ("network.site", True),
    ("network.service", True),
    ("network.asn", True),
    ("network.circuit", True),
    ("network.device", True),
    ("network.overlay", True),
    ("network.vlan", True),
    ("network.vrf", True),
    ("ai.copilot", True),
    ("compliance.conformity", True),
    ("tools.nmap", True),
)


def upgrade() -> None:
    op.create_table(
        "feature_module",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_by_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    rows = [{"id": mid, "enabled": en} for mid, en in _SEED_ROWS]
    if rows:
        op.bulk_insert(
            sa.table(
                "feature_module",
                sa.column("id", sa.String()),
                sa.column("enabled", sa.Boolean()),
            ),
            rows,
        )


def downgrade() -> None:
    op.drop_table("feature_module")
