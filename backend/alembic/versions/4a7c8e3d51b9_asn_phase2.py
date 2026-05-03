"""asn phase 2 — rdap refresh + rpki roa pull gating columns

Revision ID: 4a7c8e3d51b9
Revises: 3124d540d74f
Create Date: 2026-05-02 00:00:00.000000

Phase 2 of issue #85 lands the automation layer behind the Phase 1
schema. This migration is purely additive:

* ``asn.next_check_at`` — per-row gate for the RDAP refresh task
  (``app.tasks.asn_whois_refresh``). Beat ticks hourly; rows with
  NULL or elapsed gates get refreshed and the column is bumped
  forward by ``PlatformSettings.asn_whois_interval_hours`` (default
  24 h).
* ``asn_rpki_roa.next_check_at`` — per-row gate for the ROA pull
  task. Same shape as the parent column.
* ``asn_rpki_roa.valid_from`` / ``valid_to`` relaxed to nullable —
  the public ROA mirrors (Cloudflare's ``rpki.json``, RIPE NCC's
  validator JSON) don't surface validity windows on the wire; we
  store NULL when unknown and the alert evaluator treats NULL as
  ``state="valid"`` so it doesn't fire spurious "expired" events.
* ``platform_settings`` gains three ASN/RPKI knobs:
    - ``asn_whois_interval_hours`` (default 24, range 1..168)
    - ``rpki_roa_source`` (default ``cloudflare``, enum cloudflare|ripe)
    - ``rpki_roa_refresh_interval_hours`` (default 4, range 1..168)

No data backfill needed — ``next_check_at IS NULL`` is the documented
"refresh on next tick" sentinel for both the WHOIS and ROA tasks, so
existing rows pick up automatically once Phase 2 is deployed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4a7c8e3d51b9"
down_revision: Union[str, None] = "3124d540d74f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ASN per-row gate ───────────────────────────────────────────
    op.add_column(
        "asn",
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_asn_next_check_at", "asn", ["next_check_at"], unique=False)

    # ── ROA per-row gate + nullable validity windows ───────────────
    op.add_column(
        "asn_rpki_roa",
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_asn_rpki_roa_next_check_at",
        "asn_rpki_roa",
        ["next_check_at"],
        unique=False,
    )
    op.alter_column("asn_rpki_roa", "valid_from", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.alter_column("asn_rpki_roa", "valid_to", existing_type=sa.DateTime(timezone=True), nullable=True)

    # ── PlatformSettings — three new knobs ────────────────────────
    op.add_column(
        "platform_settings",
        sa.Column(
            "asn_whois_interval_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "rpki_roa_source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'cloudflare'"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "rpki_roa_refresh_interval_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("4"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "rpki_roa_refresh_interval_hours")
    op.drop_column("platform_settings", "rpki_roa_source")
    op.drop_column("platform_settings", "asn_whois_interval_hours")

    op.alter_column(
        "asn_rpki_roa",
        "valid_to",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "asn_rpki_roa",
        "valid_from",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_index("ix_asn_rpki_roa_next_check_at", table_name="asn_rpki_roa")
    op.drop_column("asn_rpki_roa", "next_check_at")

    op.drop_index("ix_asn_next_check_at", table_name="asn")
    op.drop_column("asn", "next_check_at")
