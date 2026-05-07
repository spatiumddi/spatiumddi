"""backup_target table for scheduled backups (issue #117 Phase 1b)

Adds the row type that Phase 1b — local volume + scheduling +
retention — and follow-up phases (1c S3, 1d SCP/Azure) all sit
on top of. One row per operator-configured destination.
``config`` carries per-kind shape (path for ``local_volume``;
S3 / SCP / Azure fields will land in 1c / 1d). ``passphrase``
is Fernet-encrypted at rest so scheduled runs don't re-prompt.

Revision ID: d2a8e417b9f3
Revises: c7e9d4f81a26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "d2a8e417b9f3"
down_revision = "c7e9d4f81a26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_target",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        # ``local_volume`` in Phase 1b. ``s3`` / ``scp`` / ``azure_blob``
        # land in 1c / 1d under the same row + driver registry.
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Per-kind config blob. Shape lives in
        # ``app.services.backup.targets.<kind>.CONFIG_FIELDS``.
        # Kept as JSONB rather than per-kind columns so adding a new
        # destination type is a single-file change.
        sa.Column(
            "config",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Fernet-encrypted backup passphrase for scheduled runs. Without
        # this, every cron tick would have to prompt the operator for
        # a passphrase — defeats scheduling. Encrypted with the
        # platform's existing Fernet helper (same key path as auth
        # provider creds + integration creds).
        sa.Column("passphrase_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("passphrase_hint", sa.String(200), nullable=False, server_default=""),
        # Optional cron expression (5-field standard, evaluated in UTC).
        # NULL means "manual only" — operator hits Run Now from the UI
        # but no scheduled tick fires.
        sa.Column("schedule_cron", sa.String(120), nullable=True),
        # Retention is mutually exclusive: keep the last N successful
        # archives OR keep archives newer than N days. Exactly one set,
        # NULL on the other. Defaults to keep_last_n=7 mirroring a
        # typical "weekly cycle" expectation.
        sa.Column("retention_keep_last_n", sa.Integer(), nullable=True),
        sa.Column("retention_keep_days", sa.Integer(), nullable=True),
        # Last-run telemetry. ``status`` mirrors the operator-facing
        # state badge: ``never`` / ``in_progress`` / ``success`` /
        # ``failed``.
        sa.Column(
            "last_run_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'never'"),
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_filename", sa.String(255), nullable=True),
        sa.Column("last_run_bytes", sa.BigInteger(), nullable=True),
        sa.Column("last_run_duration_ms", sa.Integer(), nullable=True),
        sa.Column("last_run_error", sa.Text(), nullable=True),
        # Computed by the beat sweep on schedule changes + after every
        # run; persists so the UI can show "next at <time>" without
        # re-evaluating the cron string client-side.
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Beat sweep query — find rows due in the past minute. Partial
    # index keeps the sweep cheap regardless of row count.
    op.create_index(
        "ix_backup_target_due",
        "backup_target",
        ["next_run_at"],
        postgresql_where=sa.text("enabled = true AND schedule_cron IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_backup_target_due", table_name="backup_target")
    op.drop_table("backup_target")
