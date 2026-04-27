"""NAT mapping → IPAM ip_address FKs.

Revision ID: f5b9c1e8d472
Revises: f4e1d2a09b75

Create Date: 2026-04-26 23:30:00

Adds optional ``internal_ip_address_id`` and ``external_ip_address_id``
foreign keys on ``nat_mapping``. Both nullable + ``ON DELETE SET NULL``
so the raw INET strings stay authoritative for external IPs that don't
exist in IPAM (e.g. public WAN addresses), and deleting an IPAM row
preserves the NAT mapping for forensics.

Backfill: for every existing row, look up an ``ip_address`` whose
``address`` matches the INET string and stamp the FK if found. Any row
whose IP isn't in IPAM stays NULL — no behaviour change for those.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "f5b9c1e8d472"
down_revision: str | None = "f4e1d2a09b75"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "nat_mapping",
        sa.Column("internal_ip_address_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "nat_mapping",
        sa.Column("external_ip_address_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_nat_mapping_internal_ip_address",
        "nat_mapping",
        "ip_address",
        ["internal_ip_address_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_nat_mapping_external_ip_address",
        "nat_mapping",
        "ip_address",
        ["external_ip_address_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_nat_mapping_internal_ip_address_id",
        "nat_mapping",
        ["internal_ip_address_id"],
    )
    op.create_index(
        "ix_nat_mapping_external_ip_address_id",
        "nat_mapping",
        ["external_ip_address_id"],
    )

    # Backfill — match on the INET string to ip_address.address.
    op.execute(
        """
        UPDATE nat_mapping AS n
        SET internal_ip_address_id = ip.id
        FROM ip_address AS ip
        WHERE n.internal_ip IS NOT NULL
          AND n.internal_ip_address_id IS NULL
          AND host(n.internal_ip) = host(ip.address)
        """
    )
    op.execute(
        """
        UPDATE nat_mapping AS n
        SET external_ip_address_id = ip.id
        FROM ip_address AS ip
        WHERE n.external_ip IS NOT NULL
          AND n.external_ip_address_id IS NULL
          AND host(n.external_ip) = host(ip.address)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_nat_mapping_external_ip_address_id", table_name="nat_mapping")
    op.drop_index("ix_nat_mapping_internal_ip_address_id", table_name="nat_mapping")
    op.drop_constraint("fk_nat_mapping_external_ip_address", "nat_mapping", type_="foreignkey")
    op.drop_constraint("fk_nat_mapping_internal_ip_address", "nat_mapping", type_="foreignkey")
    op.drop_column("nat_mapping", "external_ip_address_id")
    op.drop_column("nat_mapping", "internal_ip_address_id")
