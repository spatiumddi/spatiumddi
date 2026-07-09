"""wol_schedule + wol_run + wol_run_target tables + tools.wake_scheduler module (#586)

Scheduled Wake-on-LAN — Phase 1. Three tables for the recurring,
tag-targeted wake job (``wol_schedule``), its execution history
(``wol_run``), and per-host outcomes (``wol_run_target``). Plus the
``tools.wake_scheduler`` feature-module seed (default-enabled) so the
``/api/v1/wake-scheduler`` surface gates behind one toggle
(non-negotiable #14).

Phase 1 built-in holiday gate only (blackout_dates + active_from /
active_until + timezone) — no external calendar FK (that is Phase 2).

Revision ID: e9c47a1f3b28
Revises: 1440e72b9297
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e9c47a1f3b28"
down_revision: str | None = "1440e72b9297"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── wol_schedule ────────────────────────────────────────────────────
    op.create_table(
        "wol_schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "target_selector",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("schedule_cron", sa.String(length=128), nullable=True),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default=sa.text("'UTC'"),
            nullable=False,
        ),
        sa.Column(
            "blackout_dates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("active_from", sa.Date(), nullable=True),
        sa.Column("active_until", sa.Date(), nullable=True),
        sa.Column(
            "vantage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("jsonb_build_object('kind', 'server', 'id', NULL)"),
            nullable=False,
        ),
        sa.Column(
            "repeat_count",
            sa.Integer(),
            server_default=sa.text("2"),
            nullable=False,
        ),
        sa.Column(
            "repeat_interval_ms",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column(
            "stagger_ms",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "port",
            sa.Integer(),
            server_default=sa.text("9"),
            nullable=False,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(length=16), nullable=True),
        sa.Column("last_run_skip_reason", sa.String(length=32), nullable=True),
        sa.Column("last_target_count", sa.Integer(), nullable=True),
        sa.Column("in_progress_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wol_schedule_enabled_next",
        "wol_schedule",
        ["enabled", "next_run_at"],
    )

    # ── wol_run ─────────────────────────────────────────────────────────
    op.create_table(
        "wol_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "target_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "sent_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "skipped_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "failed_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "triggered_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["schedule_id"], ["wol_schedule.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wol_run_schedule_started",
        "wol_run",
        ["schedule_id", sa.text("started_at DESC")],
    )

    # ── wol_run_target ──────────────────────────────────────────────────
    op.create_table(
        "wol_run_target",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("address", sa.String(length=64), nullable=True),
        sa.Column("mac", sa.String(length=17), nullable=True),
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("broadcast", sa.String(length=45), nullable=True),
        sa.Column("vantage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mac_source", sa.String(length=16), nullable=True),
        sa.Column(
            "sent",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["wol_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ip_address_id"], ["ip_address.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wol_run_target_run",
        "wol_run_target",
        ["run_id"],
    )

    # ── feature_module seed (non-negotiable #14) ────────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('tools.wake_scheduler', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'tools.wake_scheduler'"))
    op.drop_index("ix_wol_run_target_run", table_name="wol_run_target")
    op.drop_table("wol_run_target")
    op.drop_index("ix_wol_run_schedule_started", table_name="wol_run")
    op.drop_table("wol_run")
    op.drop_index("ix_wol_schedule_enabled_next", table_name="wol_schedule")
    op.drop_table("wol_schedule")
