"""Add VLANs module — Router + VLAN tables, subnet.vlan_ref_id

Revision ID: a9b3c7d5e2f1
Revises: f8a3c1e7d925
Create Date: 2026-04-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, UUID

revision = "a9b3c7d5e2f1"
down_revision = "f8a3c1e7d925"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "router",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("location", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("management_ip", INET(), nullable=True),
        sa.Column("vendor", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
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
        sa.UniqueConstraint("name", name="uq_router_name"),
    )
    op.create_index("ix_router_name", "router", ["name"])

    op.create_table(
        "vlan",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "router_id",
            UUID(as_uuid=True),
            sa.ForeignKey("router.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
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
        sa.UniqueConstraint("router_id", "vlan_id", name="uq_vlan_router_tag"),
        sa.UniqueConstraint("router_id", "name", name="uq_vlan_router_name"),
    )
    op.create_index("ix_vlan_router_id", "vlan", ["router_id"])

    op.add_column(
        "subnet",
        sa.Column("vlan_ref_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_subnet_vlan_ref_id",
        "subnet",
        "vlan",
        ["vlan_ref_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_subnet_vlan_ref_id", "subnet", ["vlan_ref_id"])


def downgrade() -> None:
    op.drop_index("ix_subnet_vlan_ref_id", table_name="subnet")
    op.drop_constraint("fk_subnet_vlan_ref_id", "subnet", type_="foreignkey")
    op.drop_column("subnet", "vlan_ref_id")
    op.drop_index("ix_vlan_router_id", table_name="vlan")
    op.drop_table("vlan")
    op.drop_index("ix_router_name", table_name="router")
    op.drop_table("router")
