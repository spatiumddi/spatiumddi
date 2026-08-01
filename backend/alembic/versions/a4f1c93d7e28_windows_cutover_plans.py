"""Windows → SpatiumDDI cutover plans (#756)

Adds the ``migration.cutover`` feature module and its four tables — the
state behind the guided migration off a Windows DNS / DHCP estate that
the one-shot importers (#128 / #129) stop short of.

* ``cutover_plan`` — one named migration: a Windows source server (DNS
  and/or DHCP, both nullable so a plan may cover either half) and the
  SpatiumDDI target group it is moving to.
* ``cutover_item`` — one zone or one scope inside a plan. Carries
  everything a rollback needs to be exact: the last parity report, the
  last shadow-query comparison, the pristine pre-flight TTL snapshot,
  the lease-handover result, and the current blocker set.
* ``cutover_checklist_item`` — the Phase 4 decommission list, seeded per
  plan from a static catalog so ticks and notes survive a page reload.
* ``cutover_event`` — append-only timeline. No ``modified_at``: an event
  has exactly one time, the moment it happened.

``cutover_item.zone_id`` / ``scope_id`` are ``ON DELETE SET NULL`` while
``plan_id`` is ``CASCADE``. The asymmetry is deliberate: deleting a plan
is throwing away bookkeeping and should take its children with it, but
deleting the SpatiumDDI-side zone or scope mid-migration must leave the
item — and the history of what was already done to it — standing.
``source_ref`` (the Windows-side name) is the identity that survives.

Default-ENABLED per non-negotiable #14: the surface is read-mostly, and
every mutating step behind it is separately superadmin-gated. An
operator cannot turn off what they never knew shipped.

Revision ID: a4f1c93d7e28
Revises: b2d47f1c8a30
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a4f1c93d7e28"
down_revision: str | None = "b2d47f1c8a30"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── cutover_plan ───────────────────────────────────────────────────
    op.create_table(
        "cutover_plan",
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
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("source_dns_server_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_dhcp_server_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_dns_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_dhcp_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_dns_server_id"], ["dns_server.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_dhcp_server_id"], ["dhcp_server.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["target_dns_group_id"], ["dns_server_group.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["target_dhcp_group_id"], ["dhcp_server_group.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # ``name`` is declared ``unique=True, index=True`` on the model, which
    # SQLAlchemy emits as a single UNIQUE INDEX (not a separate constraint).
    op.create_index("ix_cutover_plan_name", "cutover_plan", ["name"], unique=True)
    op.create_index(
        "ix_cutover_plan_source_dns_server_id", "cutover_plan", ["source_dns_server_id"]
    )
    op.create_index(
        "ix_cutover_plan_source_dhcp_server_id", "cutover_plan", ["source_dhcp_server_id"]
    )
    op.create_index("ix_cutover_plan_target_dns_group_id", "cutover_plan", ["target_dns_group_id"])
    op.create_index(
        "ix_cutover_plan_target_dhcp_group_id", "cutover_plan", ["target_dhcp_group_id"]
    )

    # ── cutover_item ───────────────────────────────────────────────────
    op.create_table(
        "cutover_item",
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
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stage", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column(
            "blockers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_parity", postgresql.JSONB(), nullable=True),
        sa.Column("last_parity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_shadow", postgresql.JSONB(), nullable=True),
        sa.Column("last_shadow_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ttl_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("ttl_lowered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_handover", postgresql.JSONB(), nullable=True),
        sa.Column("cut_over_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["cutover_plan.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["zone_id"], ["dns_zone.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scope_id"], ["dhcp_scope.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "kind", "source_ref", name="uq_cutover_item_plan_kind_ref"),
    )
    op.create_index("ix_cutover_item_plan", "cutover_item", ["plan_id"])
    op.create_index("ix_cutover_item_zone_id", "cutover_item", ["zone_id"])
    op.create_index("ix_cutover_item_scope_id", "cutover_item", ["scope_id"])

    # ── cutover_checklist_item ─────────────────────────────────────────
    op.create_table(
        "cutover_checklist_item",
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
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("applies_to", sa.String(length=12), nullable=False, server_default="general"),
        sa.Column("is_done", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["plan_id"], ["cutover_plan.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["done_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "key", name="uq_cutover_checklist_plan_key"),
    )
    op.create_index("ix_cutover_checklist_plan", "cutover_checklist_item", ["plan_id"])

    # ── cutover_event ──────────────────────────────────────────────────
    op.create_table(
        "cutover_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("summary", sa.String(length=512), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_display_name", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["cutover_plan.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["cutover_item.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cutover_event_plan_at", "cutover_event", ["plan_id", "at"])
    op.create_index("ix_cutover_event_item_id", "cutover_event", ["item_id"])

    # ── feature_module seed (non-negotiable #14) ───────────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('migration.cutover', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'migration.cutover'"))

    op.drop_index("ix_cutover_event_item_id", table_name="cutover_event")
    op.drop_index("ix_cutover_event_plan_at", table_name="cutover_event")
    op.drop_table("cutover_event")

    op.drop_index("ix_cutover_checklist_plan", table_name="cutover_checklist_item")
    op.drop_table("cutover_checklist_item")

    op.drop_index("ix_cutover_item_scope_id", table_name="cutover_item")
    op.drop_index("ix_cutover_item_zone_id", table_name="cutover_item")
    op.drop_index("ix_cutover_item_plan", table_name="cutover_item")
    op.drop_table("cutover_item")

    op.drop_index("ix_cutover_plan_target_dhcp_group_id", table_name="cutover_plan")
    op.drop_index("ix_cutover_plan_target_dns_group_id", table_name="cutover_plan")
    op.drop_index("ix_cutover_plan_source_dhcp_server_id", table_name="cutover_plan")
    op.drop_index("ix_cutover_plan_source_dns_server_id", table_name="cutover_plan")
    op.drop_index("ix_cutover_plan_name", table_name="cutover_plan")
    op.drop_table("cutover_plan")
