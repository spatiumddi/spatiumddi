"""VRF phase 2 — asn_id FK + strict-RD-validation toggle.

Revision ID: b7e2a4f91d35
Revises: 4a9e7c2d18b3
Create Date: 2026-05-02 00:00:00.000000

Phase 2 of issue #86:

1. Adds the foreign-key constraint on ``vrf.asn_id`` referencing
   ``asn.id`` ``ON DELETE SET NULL``. The ``asn_id`` column itself
   was created by the Phase 1 migration (``2c4e9d1a7f63``) without a
   constraint because the ``asn`` table had not yet landed; with
   issue #85's ``f59a5371bdfb`` migration in place, we can wire the
   FK now. ``SET NULL`` rather than ``CASCADE`` because deleting an
   ASN typically means the operator wants to re-link the VRF to a
   replacement AS, not lose the VRF row entirely.

2. Adds the ``vrf_strict_rd_validation`` boolean column to
   ``platform_settings``. Default ``false`` — ASN-portion mismatches
   between a VRF's RD/RT and its linked ASN row produce non-blocking
   warnings on the response. Flip to ``true`` for shops that want
   the same mismatch to fail the write with 422.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2a4f91d35"
down_revision: Union[str, None] = "4a9e7c2d18b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. vrf.asn_id → asn.id FK (ON DELETE SET NULL) ────────────────────
    op.create_foreign_key(
        "fk_vrf_asn",
        "vrf",
        "asn",
        ["asn_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── 2. platform_settings.vrf_strict_rd_validation ─────────────────────
    op.add_column(
        "platform_settings",
        sa.Column(
            "vrf_strict_rd_validation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "vrf_strict_rd_validation")
    op.drop_constraint("fk_vrf_asn", "vrf", type_="foreignkey")
