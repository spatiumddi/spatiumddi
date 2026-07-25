"""restore drills: scheduled test-restore verification of backup archives

Revision ID: c9f4a71b8e35
Revises: b7e2c40a9d18
Create Date: 2026-07-24 12:00:00.000000

Issue #702 — the backup system (#117) could write archives to eight
destination kinds but never proved any of them were restorable. The
common real-world failure isn't a lost site, it's discovering at
recovery time that the archives were bad all along.

A drill downloads a target's newest archive, replays it into a
throwaway scratch database, asserts against the result, and drops the
scratch database. The live database is never touched.

Two pieces of schema:

* ``backup_target`` gains its own drill cadence — ``drill_enabled`` /
  ``drill_cron`` / ``drill_next_run_at`` mirror the existing backup
  schedule columns, deliberately separate because a drill is far more
  expensive than a backup and wants a slower cron (weekly drills
  against nightly backups is the expected shape). ``drill_last_status``
  / ``drill_last_at`` are denormalised rollups so the targets list can
  render a chip without a per-row subquery.
* ``restore_drill`` is the history of record — one row per run, with
  the per-check verdicts in a JSONB ``assertions`` list rather than a
  column per check, so adding a check later needs no migration.

Every new ``backup_target`` column carries a server default matching
the model default, so existing rows land with drills off and nothing
changes until an operator opts in.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c9f4a71b8e35"
down_revision = "b7e2c40a9d18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backup_target",
        sa.Column(
            "drill_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "backup_target",
        sa.Column("drill_cron", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "backup_target",
        sa.Column("drill_next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "backup_target",
        sa.Column(
            "drill_last_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'never'"),
        ),
    )
    op.add_column(
        "backup_target",
        sa.Column("drill_last_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "restore_drill",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "target_id",
            UUID(as_uuid=True),
            sa.ForeignKey("backup_target.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column(
            "triggered_by",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("archive_bytes", sa.BigInteger(), nullable=True),
        sa.Column("manifest", JSONB(), nullable=True),
        sa.Column("scratch_db", sa.String(length=80), nullable=True),
        sa.Column(
            "assertions",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    # Both real access patterns are per-target-newest-first: the list
    # endpoint scoped to a target, and the alert matcher's
    # ``row_number() OVER (PARTITION BY target_id ORDER BY started_at
    # DESC)``. A separate single-column index on ``target_id`` would be
    # redundant — Postgres serves ``WHERE target_id = ?`` from this
    # index's leading column at effectively the same cost, and the
    # second index would just add a write on every insert.
    op.create_index(
        "ix_restore_drill_target_started",
        "restore_drill",
        ["target_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_restore_drill_target_started", table_name="restore_drill")
    op.drop_table("restore_drill")
    op.drop_column("backup_target", "drill_last_at")
    op.drop_column("backup_target", "drill_last_status")
    op.drop_column("backup_target", "drill_next_run_at")
    op.drop_column("backup_target", "drill_cron")
    op.drop_column("backup_target", "drill_enabled")
