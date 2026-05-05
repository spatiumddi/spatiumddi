"""WAN circuits — first-class transport tracking (issue #93).

Revision ID: d9f3b21e8c54
Revises: c2a7e4f81b69
Create Date: 2026-05-05 00:00:00

Adds a single ``circuit`` table that captures the carrier-supplied
logical pipe — provider + transport class + bandwidth + endpoints +
contract term + cost. Foundation for the future MPLS L3VPN service
catalog (issue #94) and SD-WAN overlay routing (issue #95) — both
will reference this table by ``transport_class``.

FK semantics:
* ``provider_id`` is ``ON DELETE RESTRICT`` — required, and the
  carrier relationship is too load-bearing to silently null out.
* All other FKs (``customer_id``, the four endpoint refs) are
  ``ON DELETE SET NULL`` so losing a Site / Customer / Subnet
  orphans the binding rather than cascading the circuit row.

Soft-deletable: ``status='decom'`` is the operator-visible end-of-
life flag, but operators commonly want to restore a decommissioned
circuit when triaging "what carrier did Site-X use in 2024?".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "d9f3b21e8c54"
down_revision: str | None = "c2a7e4f81b69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "circuit",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("ckt_id", sa.String(length=128), nullable=True),
        sa.Column(
            "provider_id",
            UUID(as_uuid=True),
            sa.ForeignKey("provider.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customer.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "transport_class",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'internet_broadband'"),
        ),
        sa.Column(
            "bandwidth_mbps_down",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "bandwidth_mbps_up",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "a_end_site_id",
            UUID(as_uuid=True),
            sa.ForeignKey("site.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "a_end_subnet_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subnet.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "z_end_site_id",
            UUID(as_uuid=True),
            sa.ForeignKey("site.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "z_end_subnet_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subnet.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("term_start_date", sa.Date(), nullable=True),
        sa.Column("term_end_date", sa.Date(), nullable=True),
        sa.Column("monthly_cost", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default=sa.text("'USD'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
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
        sa.Column("previous_status", sa.String(length=16), nullable=True),
        sa.Column(
            "last_status_change_at", sa.DateTime(timezone=True), nullable=True
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
        # Soft-delete columns (mirrors IPSpace / IPBlock / Subnet).
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deleted_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deletion_batch_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_circuit_provider_id", "circuit", ["provider_id"])
    op.create_index("ix_circuit_customer_id", "circuit", ["customer_id"])
    op.create_index("ix_circuit_a_end_site_id", "circuit", ["a_end_site_id"])
    op.create_index("ix_circuit_z_end_site_id", "circuit", ["z_end_site_id"])
    op.create_index("ix_circuit_a_end_subnet_id", "circuit", ["a_end_subnet_id"])
    op.create_index("ix_circuit_z_end_subnet_id", "circuit", ["z_end_subnet_id"])
    op.create_index("ix_circuit_transport_class", "circuit", ["transport_class"])
    op.create_index("ix_circuit_status", "circuit", ["status"])
    op.create_index("ix_circuit_term_end_date", "circuit", ["term_end_date"])
    op.create_index("ix_circuit_ckt_id", "circuit", ["ckt_id"])
    op.create_index("ix_circuit_deleted_at", "circuit", ["deleted_at"])
    op.create_index("ix_circuit_deletion_batch_id", "circuit", ["deletion_batch_id"])


def downgrade() -> None:
    op.drop_index("ix_circuit_deletion_batch_id", table_name="circuit")
    op.drop_index("ix_circuit_deleted_at", table_name="circuit")
    op.drop_index("ix_circuit_ckt_id", table_name="circuit")
    op.drop_index("ix_circuit_term_end_date", table_name="circuit")
    op.drop_index("ix_circuit_status", table_name="circuit")
    op.drop_index("ix_circuit_transport_class", table_name="circuit")
    op.drop_index("ix_circuit_z_end_subnet_id", table_name="circuit")
    op.drop_index("ix_circuit_a_end_subnet_id", table_name="circuit")
    op.drop_index("ix_circuit_z_end_site_id", table_name="circuit")
    op.drop_index("ix_circuit_a_end_site_id", table_name="circuit")
    op.drop_index("ix_circuit_customer_id", table_name="circuit")
    op.drop_index("ix_circuit_provider_id", table_name="circuit")
    op.drop_table("circuit")
