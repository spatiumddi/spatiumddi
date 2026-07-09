"""wol verify/retry columns on wol_schedule / wol_run / wol_run_target (#586 Phase 3)

Scheduled Wake-on-LAN — Phase 3 (post-wake liveness verify + bounded retry).
Pure-additive column adds; no new tables.

* ``wol_schedule`` — verify config: ``verify_enabled`` (bool),
  ``verify_wait_seconds`` (int), ``verify_retries`` (int), ``verify_method``
  (str, 'ping' in v1).
* ``wol_run`` — verify rollup: ``verify_state`` (none|pending|verifying|done),
  ``verified_count`` (int), ``unverified_count`` (int), plus the crash-recovery
  mutex columns ``verify_claimed_at`` (nullable ts — the verify lease) and
  ``verify_attempt`` (int, ≥1 — the run-level attempt anchor the reaper +
  attempt-guarded claim key on).
* ``wol_run_target`` — per-host outcome: ``verified`` (nullable tri-state),
  ``verified_at`` (ts), ``verify_method`` (str), ``wake_attempts`` (int, ≥1).

Every NOT NULL column carries a ``server_default`` so the add backfills existing
rows on a populated table (fresh-install safety). The three nullable
``wol_run_target`` verify columns take no default (NULL == not-yet/not-checked).

Reuses the Phase-1 ``tools.wake_scheduler`` feature module (NO new module seed).

Revision ID: b4f2a9c17e63
Revises: d7e3f0a91c24
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b4f2a9c17e63"
down_revision: str | None = "d7e3f0a91c24"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── wol_schedule — verify config ────────────────────────────────────
    op.add_column(
        "wol_schedule",
        sa.Column(
            "verify_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "wol_schedule",
        sa.Column(
            "verify_wait_seconds",
            sa.Integer(),
            server_default=sa.text("60"),
            nullable=False,
        ),
    )
    op.add_column(
        "wol_schedule",
        sa.Column(
            "verify_retries",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "wol_schedule",
        sa.Column(
            "verify_method",
            sa.String(length=16),
            server_default=sa.text("'ping'"),
            nullable=False,
        ),
    )

    # ── wol_run — verify rollup ─────────────────────────────────────────
    op.add_column(
        "wol_run",
        sa.Column(
            "verify_state",
            sa.String(length=16),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
    )
    op.add_column(
        "wol_run",
        sa.Column(
            "verified_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "wol_run",
        sa.Column(
            "unverified_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    # Verify mutex lease (nullable — NULL == never armed) + attempt anchor.
    op.add_column(
        "wol_run",
        sa.Column("verify_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wol_run",
        sa.Column(
            "verify_attempt",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )

    # ── wol_run_target — per-host verify outcome ────────────────────────
    op.add_column(
        "wol_run_target",
        sa.Column("verified", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "wol_run_target",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wol_run_target",
        sa.Column("verify_method", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "wol_run_target",
        sa.Column(
            "wake_attempts",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("wol_run_target", "wake_attempts")
    op.drop_column("wol_run_target", "verify_method")
    op.drop_column("wol_run_target", "verified_at")
    op.drop_column("wol_run_target", "verified")
    op.drop_column("wol_run", "verify_attempt")
    op.drop_column("wol_run", "verify_claimed_at")
    op.drop_column("wol_run", "unverified_count")
    op.drop_column("wol_run", "verified_count")
    op.drop_column("wol_run", "verify_state")
    op.drop_column("wol_schedule", "verify_method")
    op.drop_column("wol_schedule", "verify_retries")
    op.drop_column("wol_schedule", "verify_wait_seconds")
    op.drop_column("wol_schedule", "verify_enabled")
