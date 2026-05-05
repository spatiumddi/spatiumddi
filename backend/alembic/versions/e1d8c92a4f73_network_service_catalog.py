"""Service catalog — first-class customer-deliverable bundles (issue #94).

Revision ID: e1d8c92a4f73
Revises: d9f3b21e8c54
Create Date: 2026-05-05 00:00:00

Adds two tables:

* ``network_service`` — one row per customer / team deliverable. The
  first concrete ``kind`` is ``mpls_l3vpn``; ``custom`` is the catch-
  all. Other kinds reserve names in the application enum but the
  column itself is plain ``String(32)`` so future kinds don't need a
  column migration.
* ``network_service_resource`` — polymorphic join row binding a service
  to a core entity (VRF / Subnet / IPBlock / DNSZone / DHCPScope /
  Circuit / Site / OverlayNetwork). ``resource_id`` is *not* a true
  FK — the router validates targets at attach time and the
  ``service_resource_orphaned`` alert (Wave 2) sweeps stale rows.

FK semantics:

* ``customer_id`` is ``ON DELETE RESTRICT`` — a customer has too much
  contractual weight to silently null out from under a service. The
  operator must detach / re-customer services first.
* ``service_id`` on the join row is ``ON DELETE CASCADE`` — when a
  service is hard-deleted (the rare case after soft-delete trash
  expiry), its bindings go too.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "e1d8c92a4f73"
down_revision: str | None = "d9f3b21e8c54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "network_service",
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
            server_default=sa.text("'custom'"),
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customer.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'provisioning'"),
        ),
        sa.Column("term_start_date", sa.Date(), nullable=True),
        sa.Column("term_end_date", sa.Date(), nullable=True),
        sa.Column("monthly_cost_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default=sa.text("'USD'"),
        ),
        sa.Column("sla_tier", sa.String(length=32), nullable=True),
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
        sa.UniqueConstraint("customer_id", "name", name="uq_network_service_customer_name"),
    )
    op.create_index("ix_network_service_customer_id", "network_service", ["customer_id"])
    op.create_index("ix_network_service_kind", "network_service", ["kind"])
    op.create_index("ix_network_service_status", "network_service", ["status"])
    op.create_index("ix_network_service_term_end_date", "network_service", ["term_end_date"])
    op.create_index("ix_network_service_deleted_at", "network_service", ["deleted_at"])
    op.create_index(
        "ix_network_service_deletion_batch_id",
        "network_service",
        ["deletion_batch_id"],
    )

    op.create_table(
        "network_service_resource",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            UUID(as_uuid=True),
            sa.ForeignKey("network_service.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=True),
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
            "service_id",
            "resource_kind",
            "resource_id",
            name="uq_network_service_resource_triple",
        ),
    )
    op.create_index(
        "ix_nsr_service_kind",
        "network_service_resource",
        ["service_id", "resource_kind"],
    )
    op.create_index(
        "ix_nsr_kind_target",
        "network_service_resource",
        ["resource_kind", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_nsr_kind_target", table_name="network_service_resource")
    op.drop_index("ix_nsr_service_kind", table_name="network_service_resource")
    op.drop_table("network_service_resource")

    op.drop_index("ix_network_service_deletion_batch_id", table_name="network_service")
    op.drop_index("ix_network_service_deleted_at", table_name="network_service")
    op.drop_index("ix_network_service_term_end_date", table_name="network_service")
    op.drop_index("ix_network_service_status", table_name="network_service")
    op.drop_index("ix_network_service_kind", table_name="network_service")
    op.drop_index("ix_network_service_customer_id", table_name="network_service")
    op.drop_table("network_service")
