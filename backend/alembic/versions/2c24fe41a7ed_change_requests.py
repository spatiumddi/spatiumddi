"""change_request + approval_policy tables + governance.approvals module (#62)

Two-person approval workflow substrate: ``change_request`` holds a risky
operation queued for second-person approval, ``approval_policy`` holds the
operator-tunable rules deciding when the gate fires. Built-in policy rows
seed ``enabled=False`` so existing installs see zero behaviour change until
an operator opts in. The ``governance.approvals`` feature_module row seeds
``FALSE`` (default-off, non-negotiable #14). The "Change Approver" builtin
role is seeded in main.py startup, not here.

Revision ID: 2c24fe41a7ed
Revises: e2b9c4f1a7d6
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2c24fe41a7ed"
down_revision: str | None = "e2b9c4f1a7d6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "change_request",
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
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("resource_display", sa.String(length=500), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preview_text", sa.Text(), nullable=False),
        sa.Column("risk_reason", sa.String(length=255), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_display", sa.String(length=255), nullable=False),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_display", sa.String(length=255), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_change_request_state_expires", "change_request", ["state", "expires_at"])
    op.create_index("ix_change_request_requested_by", "change_request", ["requested_by_user_id"])
    op.create_index(
        "ix_change_request_resource", "change_request", ["resource_type", "resource_id"]
    )

    op.create_table(
        "approval_policy",
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
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("min_count", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "applies_to_superadmin",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("ttl_hours", sa.Integer(), server_default=sa.text("168"), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approval_policy_match", "approval_policy", ["resource_type", "action"])

    # ── feature_module seed (non-negotiable #14) — default-off ───────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('governance.approvals', FALSE)
            ON CONFLICT (id) DO NOTHING
            """))

    # ── built-in approval policies — all enabled=FALSE so existing ───────
    # installs see zero behaviour change until an operator opts in.
    op.execute(sa.text("""
            INSERT INTO approval_policy
                (id, name, resource_type, action, min_count,
                 enabled, applies_to_superadmin, ttl_hours, is_builtin)
            VALUES
                (gen_random_uuid(), 'Delete subnet', 'subnet', 'delete',
                 NULL, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Delete IP block', 'ip_block', 'delete',
                 NULL, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Delete IP space', 'ip_space', 'delete',
                 NULL, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Delete DNS zone', 'dns_zone', 'delete',
                 NULL, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Delete DHCP scope', 'dhcp_scope', 'delete',
                 NULL, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Delete DHCP server group',
                 'dhcp_server_group', 'delete', NULL, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Bulk delete (>= 25)', '*', 'bulk_delete',
                 25, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Bulk edit (>= 50)', '*', 'bulk_edit',
                 50, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Bulk allocate (>= 256)', '*',
                 'bulk_allocate', 256, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Factory reset', 'platform',
                 'factory_reset', NULL, FALSE, TRUE, 168, TRUE),
                (gen_random_uuid(), 'Import commit (>= 100)', '*',
                 'import_commit', 100, FALSE, TRUE, 168, TRUE)
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'governance.approvals'"))
    op.drop_index("ix_approval_policy_match", table_name="approval_policy")
    op.drop_table("approval_policy")
    op.drop_index("ix_change_request_resource", table_name="change_request")
    op.drop_index("ix_change_request_requested_by", table_name="change_request")
    op.drop_index("ix_change_request_state_expires", table_name="change_request")
    op.drop_table("change_request")
