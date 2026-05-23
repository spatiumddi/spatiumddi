"""system_upgrade_run table (#296 Phase A — rolling-upgrade state)

One row per cluster-wide or single-node upgrade attempt. Phase A only
writes a ``planned`` row from the read-only preflight endpoint; Phases
C/D drive the lifecycle through running → succeeded | failed | halted
| aborted as the orchestrator walks per-node.

The cluster-wide single-upgrader mutex lives in a
``coordination.k8s.io/v1/Lease``, NOT in this table — the lease holder
is recorded here only for audit. Same row also captures the plan +
per-node progress as JSONB so the orchestrator's state machine can
grow new steps without DB migrations.

Revision ID: a8e3f127c094
Revises: a1c7e9f32b84
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a8e3f127c094"
down_revision = "a1c7e9f32b84"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_upgrade_run",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'planned'"),
        ),
        sa.Column("target_version", sa.String(length=64), nullable=False),
        sa.Column(
            "source_versions",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "plan",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "progress",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("lease_holder", sa.String(length=128), nullable=True),
        sa.Column(
            "lease_acquired_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "started_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_system_upgrade_run_state",
        "system_upgrade_run",
        ["state"],
    )
    # Cluster-wide invariant: at most one non-terminal run at a time.
    # The k8s Lease is the real lock (survives DB blips); this index
    # is a backstop so a bug in the orchestrator can't race two rows
    # into ``running`` simultaneously. Terminal states (succeeded,
    # failed, halted, aborted) are excluded from the partial index so
    # operators can keep full upgrade history without bumping into it.
    op.execute(
        "CREATE UNIQUE INDEX ix_system_upgrade_run_one_active "
        "ON system_upgrade_run ((1)) "
        "WHERE state IN ('planned', 'running', 'halted')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_system_upgrade_run_one_active")
    op.drop_index(
        "ix_system_upgrade_run_state",
        table_name="system_upgrade_run",
    )
    op.drop_table("system_upgrade_run")
