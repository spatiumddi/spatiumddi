"""SD-WAN overlay topology — overlays + sites + policies + apps (#95).

Revision ID: c4f7e92d3a18
Revises: f2c8d49a1e76
Create Date: 2026-05-05 00:00:00

Four tables landing together:

* ``overlay_network`` — soft-deletable logical overlay (one per
  customer / internal mesh). Optional ``customer_id`` ``ON DELETE SET
  NULL``.
* ``overlay_site`` — m2m through site membership. Cascade off the
  overlay; nullify off the site / device / loopback subnet so a
  resource delete doesn't take the membership row with it.
* ``routing_policy`` — declarative per-overlay policy (priority + match
  + action). Cascade off the overlay.
* ``application_category`` — curated SaaS catalog. Seeded at startup
  by ``services.applications.seed_builtin_applications``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "c4f7e92d3a18"
down_revision: str | None = "f2c8d49a1e76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "overlay_network",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'sdwan'"),
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customer.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("vendor", sa.String(length=64), nullable=True),
        sa.Column("encryption_profile", sa.String(length=128), nullable=True),
        sa.Column(
            "default_path_strategy",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'active_backup'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'building'"),
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "tags",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "custom_fields",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deleted_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deletion_batch_id", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_overlay_network_name"),
    )
    op.create_index("ix_overlay_network_customer_id", "overlay_network", ["customer_id"])
    op.create_index("ix_overlay_network_kind", "overlay_network", ["kind"])
    op.create_index("ix_overlay_network_status", "overlay_network", ["status"])
    op.create_index("ix_overlay_network_deleted_at", "overlay_network", ["deleted_at"])
    op.create_index(
        "ix_overlay_network_deletion_batch_id",
        "overlay_network",
        ["deletion_batch_id"],
    )

    op.create_table(
        "overlay_site",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "overlay_network_id",
            UUID(as_uuid=True),
            sa.ForeignKey("overlay_network.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            UUID(as_uuid=True),
            sa.ForeignKey("site.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'spoke'"),
        ),
        sa.Column(
            "device_id",
            UUID(as_uuid=True),
            sa.ForeignKey("network_device.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "loopback_subnet_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subnet.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "preferred_circuits",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
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
        sa.UniqueConstraint(
            "overlay_network_id",
            "site_id",
            name="uq_overlay_site_overlay_site",
        ),
    )
    op.create_index("ix_overlay_site_overlay_id", "overlay_site", ["overlay_network_id"])
    op.create_index("ix_overlay_site_site_id", "overlay_site", ["site_id"])
    op.create_index("ix_overlay_site_role", "overlay_site", ["role"])

    op.create_table(
        "routing_policy",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "overlay_network_id",
            UUID(as_uuid=True),
            sa.ForeignKey("overlay_network.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
        sa.Column("match_kind", sa.String(length=24), nullable=False),
        sa.Column("match_value", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("action_target", sa.String(length=255), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
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
    )
    op.create_index(
        "ix_routing_policy_overlay_id", "routing_policy", ["overlay_network_id"]
    )
    op.create_index(
        "ix_routing_policy_overlay_priority",
        "routing_policy",
        ["overlay_network_id", "priority"],
    )

    op.create_table(
        "application_category",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("default_dscp", sa.Integer(), nullable=True),
        sa.Column(
            "category",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'saas'"),
        ),
        sa.Column(
            "is_builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
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
        sa.UniqueConstraint("name", name="uq_application_category_name"),
    )


def downgrade() -> None:
    op.drop_table("application_category")
    op.drop_index("ix_routing_policy_overlay_priority", table_name="routing_policy")
    op.drop_index("ix_routing_policy_overlay_id", table_name="routing_policy")
    op.drop_table("routing_policy")
    op.drop_index("ix_overlay_site_role", table_name="overlay_site")
    op.drop_index("ix_overlay_site_site_id", table_name="overlay_site")
    op.drop_index("ix_overlay_site_overlay_id", table_name="overlay_site")
    op.drop_table("overlay_site")
    op.drop_index(
        "ix_overlay_network_deletion_batch_id", table_name="overlay_network"
    )
    op.drop_index("ix_overlay_network_deleted_at", table_name="overlay_network")
    op.drop_index("ix_overlay_network_status", table_name="overlay_network")
    op.drop_index("ix_overlay_network_kind", table_name="overlay_network")
    op.drop_index("ix_overlay_network_customer_id", table_name="overlay_network")
    op.drop_table("overlay_network")
