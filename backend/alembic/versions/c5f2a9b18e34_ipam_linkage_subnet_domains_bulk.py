"""ipam: ip_address linkage fields + subnet_domain junction table

Revision ID: c5f2a9b18e34
Revises: b7e3f1c8d2a4
Create Date: 2026-04-14 15:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c5f2a9b18e34"
down_revision = "c3f1e7b92a5d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ip_address linkage fields (§3) ────────────────────────────────────────
    op.add_column(
        "ip_address",
        sa.Column("forward_zone_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ip_address",
        sa.Column("reverse_zone_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ip_address",
        sa.Column("dns_record_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("ip_address", sa.Column("dhcp_lease_id", sa.String(255), nullable=True))
    op.add_column(
        "ip_address", sa.Column("static_assignment_id", sa.String(255), nullable=True)
    )
    op.create_foreign_key(
        "fk_ip_address_forward_zone",
        "ip_address",
        "dns_zone",
        ["forward_zone_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ip_address_reverse_zone",
        "ip_address",
        "dns_zone",
        ["reverse_zone_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ip_address_dns_record",
        "ip_address",
        "dns_record",
        ["dns_record_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── subnet_domain junction (§11) ──────────────────────────────────────────
    op.create_table(
        "subnet_domain",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dns_zone_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "is_primary", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
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
        sa.ForeignKeyConstraint(
            ["subnet_id"], ["subnet.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["dns_zone_id"], ["dns_zone.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "subnet_id", "dns_zone_id", name="uq_subnet_domain_subnet_zone"
        ),
    )
    op.create_index(
        "ix_subnet_domain_subnet_id", "subnet_domain", ["subnet_id"]
    )
    op.create_index(
        "ix_subnet_domain_dns_zone_id", "subnet_domain", ["dns_zone_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_subnet_domain_dns_zone_id", table_name="subnet_domain")
    op.drop_index("ix_subnet_domain_subnet_id", table_name="subnet_domain")
    op.drop_table("subnet_domain")

    op.drop_constraint("fk_ip_address_dns_record", "ip_address", type_="foreignkey")
    op.drop_constraint("fk_ip_address_reverse_zone", "ip_address", type_="foreignkey")
    op.drop_constraint("fk_ip_address_forward_zone", "ip_address", type_="foreignkey")
    op.drop_column("ip_address", "static_assignment_id")
    op.drop_column("ip_address", "dhcp_lease_id")
    op.drop_column("ip_address", "dns_record_id")
    op.drop_column("ip_address", "reverse_zone_id")
    op.drop_column("ip_address", "forward_zone_id")
