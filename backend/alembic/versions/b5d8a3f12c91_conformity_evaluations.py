"""Conformity evaluations — policy + result tables (issue #106).

Two tables:

* ``conformity_policy`` — declarative policies. ``is_builtin=True``
  marks the seeded library; operators can add their own with
  ``is_builtin=False``. The ``check_kind`` column names a Python
  evaluator function in ``app.services.conformity.checks``.

* ``conformity_result`` — append-only history. One row per
  ``(policy, resource)`` per pass. Indexed twice (by policy and by
  resource) so both natural drilldowns hit an index without
  competing.

Revision ID: b5d8a3f12c91
Revises: e3f1c92a4d68
Create Date: 2026-05-05 10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b5d8a3f12c91"
down_revision = "e3f1c92a4d68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conformity_policy",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("framework", sa.String(length=40), nullable=False, server_default="custom"),
        sa.Column("reference", sa.String(length=80), nullable=True),
        sa.Column("severity", sa.String(length=10), nullable=False, server_default="warning"),
        sa.Column("target_kind", sa.String(length=40), nullable=False),
        sa.Column("target_filter", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("check_kind", sa.String(length=60), nullable=False),
        sa.Column("check_args", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "eval_interval_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fail_alert_rule_id",
            sa.UUID(),
            sa.ForeignKey("alert_rule.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_conformity_policy_name", "conformity_policy", ["name"])
    op.create_index(
        "ix_conformity_policy_framework_enabled",
        "conformity_policy",
        ["framework", "enabled"],
    )

    op.create_table(
        "conformity_result",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "policy_id",
            sa.UUID(),
            sa.ForeignKey("conformity_policy.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_kind", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column(
            "resource_display",
            sa.String(length=500),
            nullable=False,
            server_default="",
        ),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("diagnostic", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_conformity_result_policy_evaluated",
        "conformity_result",
        ["policy_id", "evaluated_at"],
    )
    op.create_index(
        "ix_conformity_result_resource_evaluated",
        "conformity_result",
        ["resource_kind", "resource_id", "evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conformity_result_resource_evaluated",
        table_name="conformity_result",
    )
    op.drop_index(
        "ix_conformity_result_policy_evaluated",
        table_name="conformity_result",
    )
    op.drop_table("conformity_result")
    op.drop_index(
        "ix_conformity_policy_framework_enabled",
        table_name="conformity_policy",
    )
    op.drop_index("ix_conformity_policy_name", table_name="conformity_policy")
    op.drop_table("conformity_policy")
