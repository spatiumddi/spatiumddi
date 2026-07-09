"""wol_calendar + wol_calendar_event + wol_schedule.calendar_* (#586 Phase 2)

Scheduled Wake-on-LAN — Phase 2 (external calendar gate). Adds:

* ``wol_calendar`` — a subscribed iCal ``.ics`` URL or authenticated CalDAV
  collection (Fernet-encrypted password, sync-status mirror).
* ``wol_calendar_event`` — flattened all-day event spans pulled from a
  calendar (recurrence expanded over a bounded horizon) for O(events) gate
  checks + a UI preview.
* three columns on ``wol_schedule`` — ``calendar_id`` (FK → wol_calendar,
  ON DELETE SET NULL), ``calendar_mode`` (none | skip_on_event | only_on_event),
  ``calendar_match`` (optional summary/category regex).

Reuses the Phase-1 ``tools.wake_scheduler`` feature module (NO new module seed).

Revision ID: d7e3f0a91c24
Revises: e9c47a1f3b28
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d7e3f0a91c24"
down_revision: str | None = "e9c47a1f3b28"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── wol_calendar ────────────────────────────────────────────────────
    op.create_table(
        "wol_calendar",
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
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("password_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "refresh_interval_minutes",
            sa.Integer(),
            server_default=sa.text("360"),
            nullable=False,
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(length=16), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column(
            "event_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wol_calendar_name", "wol_calendar", ["name"])

    # ── wol_calendar_event ──────────────────────────────────────────────
    op.create_table(
        "wol_calendar_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("calendar_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "categories",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("uid", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["calendar_id"], ["wol_calendar.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE (NULLS NOT DISTINCT, PG15+) on the reconcile natural key so a
    # concurrent inline sync-now + beat sweep can't leak a permanent duplicate
    # span row (``uid`` is nullable, hence NULLS NOT DISTINCT). Leading columns
    # also serve the gate load + upcoming-events span queries.
    op.create_index(
        "uq_wol_calendar_event_natural",
        "wol_calendar_event",
        ["calendar_id", "starts_on", "ends_on", "uid"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    # ── wol_schedule.calendar_* ─────────────────────────────────────────
    op.add_column(
        "wol_schedule",
        sa.Column("calendar_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "wol_schedule",
        sa.Column(
            "calendar_mode",
            sa.String(length=16),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
    )
    op.add_column(
        "wol_schedule",
        sa.Column("calendar_match", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_wol_schedule_calendar_id",
        "wol_schedule",
        "wol_calendar",
        ["calendar_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_wol_schedule_calendar_id", "wol_schedule", type_="foreignkey")
    op.drop_column("wol_schedule", "calendar_match")
    op.drop_column("wol_schedule", "calendar_mode")
    op.drop_column("wol_schedule", "calendar_id")
    op.drop_index("uq_wol_calendar_event_natural", table_name="wol_calendar_event")
    op.drop_table("wol_calendar_event")
    op.drop_index("ix_wol_calendar_name", table_name="wol_calendar")
    op.drop_table("wol_calendar")
