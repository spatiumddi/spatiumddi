"""Issue #285 Phase 3a — firewall policy data model (schema only).

Three tables for the declarative fleet-firewall policy model:

* ``firewall_policy`` — a layer of rules at one scope (fleet singleton /
  per-role / per-appliance).
* ``firewall_rule`` — a rule within a policy (compiles to a family-split
  nft fragment; ``source_kind`` may be a derived scope resolved per-node).
* ``firewall_alias`` — a named, family-split CIDR-set or port-set.

Pure additive: no code reads these yet (the merge engine in 3b wires them;
the builtin seed lands in the companion 3a seed migration). Zero runtime
change. The full Phase-3 column set lands here so the tables migrate once.

Revision ID: e4a7c1f08b9d
Revises: a3f1d9e07c52
Create Date: 2026-06-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "e4a7c1f08b9d"
down_revision: str | None = "a3f1d9e07c52"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "firewall_policy",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("scope_role", sa.String(length=32), nullable=True),
        sa.Column("scope_appliance_id", UUID(as_uuid=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("updated_by_id", UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(["scope_appliance_id"], ["appliance.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_kind", "scope_role", name="uq_fw_policy_role"),
        sa.CheckConstraint(
            "(scope_kind='fleet' AND scope_role IS NULL AND scope_appliance_id IS NULL) OR "
            "(scope_kind='role' AND scope_role IS NOT NULL AND scope_appliance_id IS NULL) OR "
            "(scope_kind='appliance' AND scope_appliance_id IS NOT NULL AND scope_role IS NULL)",
            name="ck_fw_policy_scope_shape",
        ),
    )
    op.create_index("ix_fw_policy_scope", "firewall_policy", ["scope_kind", "enabled"])
    op.create_index(
        "uq_fw_policy_appliance",
        "firewall_policy",
        ["scope_appliance_id"],
        unique=True,
        postgresql_where=sa.text("scope_kind = 'appliance'"),
    )
    op.create_index(
        "uq_fw_policy_fleet_singleton",
        "firewall_policy",
        ["scope_kind"],
        unique=True,
        postgresql_where=sa.text("scope_kind = 'fleet'"),
    )

    op.create_table(
        "firewall_rule",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("policy_id", UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "action", sa.String(length=8), nullable=False, server_default=sa.text("'accept'")
        ),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("ports", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "source_kind", sa.String(length=16), nullable=False, server_default=sa.text("'any'")
        ),
        sa.Column("source_cidrs", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_alias", sa.String(length=64), nullable=True),
        sa.Column("family", sa.String(length=6), nullable=False, server_default=sa.text("'both'")),
        sa.Column("comment", sa.String(length=120), nullable=True),
        sa.Column("render_guard", JSONB(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(["policy_id"], ["firewall_policy.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_id", "seq", name="uq_fw_rule_policy_seq"),
        sa.CheckConstraint(
            "NOT (action = 'drop' AND ports @> '22'::jsonb)",
            name="ck_fw_rule_no_drop_ssh",
        ),
    )
    op.create_index("ix_fw_rule_policy_seq", "firewall_rule", ["policy_id", "seq"])

    op.create_table(
        "firewall_alias",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("port_members", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("v4_members", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("v6_members", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_firewall_alias_name", "firewall_alias", ["name"], unique=True)


def downgrade() -> None:
    op.drop_table("firewall_alias")
    op.drop_table("firewall_rule")
    op.drop_table("firewall_policy")
