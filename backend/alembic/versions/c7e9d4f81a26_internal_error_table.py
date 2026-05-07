"""internal_error table for unhandled exception capture (issue #123)

Captures every uncaught exception from the API + Celery workers into
a queryable table so operators can review crashes without tailing
``docker compose logs``. The dedup loop in
``app.services.diagnostics.capture`` looks up by ``fingerprint``
(sha256 of exception class + top-2 frames) and either bumps
``occurrence_count`` / ``last_seen_at`` on a match within the
suppression window, or inserts a new row.

Revision ID: c7e9d4f81a26
Revises: f1a3b8c52d04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "c7e9d4f81a26"
down_revision = "f1a3b8c52d04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "internal_error",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Distinguishes which container raised the exception. Operators
        # filter on this in the admin viewer.
        sa.Column("service", sa.String(20), nullable=False),
        # Reserved for future expansion (e.g. ``frontend_error`` once
        # the frontend capture lands as a follow-up). Today's only
        # value is ``unhandled_exception``.
        sa.Column(
            "kind",
            sa.String(40),
            nullable=False,
            server_default=sa.text("'unhandled_exception'"),
        ),
        sa.Column("request_id", sa.String(64), nullable=True),
        # HTTP route or Celery task name, whichever applies. NULL
        # when neither (e.g. exception during app startup).
        sa.Column("route_or_task", sa.String(255), nullable=True),
        sa.Column("exception_class", sa.String(255), nullable=False),
        # One-line message off the exception. Kept short so the list
        # view doesn't wrap; the full message is in ``traceback``.
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=False),
        # Sanitised: stripped Authorization / Cookie / X-API-Token /
        # password-shaped fields; bodies > 4 KB replaced with a
        # truncation marker; whole blob capped at 16 KB.
        sa.Column(
            "context_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # sha256 hex of (exception_class, top-2 frames) for grouping.
        # The capture loop uses this to dedupe noisy crashes.
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("acknowledged_by", UUID(as_uuid=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        # While ``suppressed_until > now()`` the capture loop bumps
        # the matching fingerprint's ``occurrence_count`` instead of
        # inserting a new row. Operators set this with the
        # "Suppress 24h" button in the admin viewer.
        sa.Column("suppressed_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["acknowledged_by"], ["user.id"], ondelete="SET NULL"),
    )
    # Recent-first list view + filter on service.
    op.create_index(
        "ix_internal_error_timestamp",
        "internal_error",
        [sa.text("timestamp DESC")],
    )
    op.create_index("ix_internal_error_service", "internal_error", ["service"])
    # Dedup lookup: fingerprint + suppression-window check.
    op.create_index(
        "ix_internal_error_fingerprint",
        "internal_error",
        ["fingerprint"],
    )
    # Unacked-count surface for the floating banner. Partial index keeps
    # the count cheap regardless of total table size.
    op.create_index(
        "ix_internal_error_unacked",
        "internal_error",
        ["last_seen_at"],
        postgresql_where=sa.text("acknowledged_by IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_internal_error_unacked", table_name="internal_error")
    op.drop_index("ix_internal_error_fingerprint", table_name="internal_error")
    op.drop_index("ix_internal_error_service", table_name="internal_error")
    op.drop_index("ix_internal_error_timestamp", table_name="internal_error")
    op.drop_table("internal_error")
