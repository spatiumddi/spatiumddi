"""BGP ASN FK on ip_space and ip_block.

Revision ID: c9f1e47d2a83
Revises: b7e2a4f91d35
Create Date: 2026-05-03 00:00:00.000000

Optional FK from ``ip_space.asn_id`` and ``ip_block.asn_id`` to the
``asn`` table (added by issue #85). NULL means "no BGP origin
recorded". ``ON DELETE SET NULL`` so deleting an ASN row leaves the
space / block intact — typical operator intent is to relink to a
replacement AS, not lose the IPAM hierarchy.

Indexed on each side because BGP-aware reports filter by
``asn_id`` (e.g. "every prefix announced by AS65001").
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c9f1e47d2a83"
down_revision: Union[str, None] = "b7e2a4f91d35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ip_space",
        sa.Column("asn_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_ip_space_asn_id",
        "ip_space",
        ["asn_id"],
    )
    op.create_foreign_key(
        "fk_ip_space_asn",
        "ip_space",
        "asn",
        ["asn_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "ip_block",
        sa.Column("asn_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_ip_block_asn_id",
        "ip_block",
        ["asn_id"],
    )
    op.create_foreign_key(
        "fk_ip_block_asn",
        "ip_block",
        "asn",
        ["asn_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_ip_block_asn", "ip_block", type_="foreignkey")
    op.drop_index("ix_ip_block_asn_id", table_name="ip_block")
    op.drop_column("ip_block", "asn_id")
    op.drop_constraint("fk_ip_space_asn", "ip_space", type_="foreignkey")
    op.drop_index("ix_ip_space_asn_id", table_name="ip_space")
    op.drop_column("ip_space", "asn_id")
