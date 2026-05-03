"""DNSZone.domain_id FK to domain table.

Revision ID: e7b8c4f96a12
Revises: d3f2a51c8e76
Create Date: 2026-05-03 02:00:00.000000

Optional explicit link from a DNS zone to its registered domain (issue
#87). The Domain detail page falls back to a name-match heuristic when
this is NULL, so existing zones don't need backfilling — operators can
opt in by setting it on zone create / edit.

``ON DELETE SET NULL`` because deleting a Domain row should not
cascade-drop the DNS zone — the zone may still need to exist while the
operator re-imports a fresh Domain row from RDAP.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "e7b8c4f96a12"
down_revision: Union[str, None] = "d3f2a51c8e76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dns_zone",
        sa.Column("domain_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_dns_zone_domain_id", "dns_zone", ["domain_id"])
    op.create_foreign_key(
        "fk_dns_zone_domain",
        "dns_zone",
        "domain",
        ["domain_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_dns_zone_domain", "dns_zone", type_="foreignkey")
    op.drop_index("ix_dns_zone_domain_id", table_name="dns_zone")
    op.drop_column("dns_zone", "domain_id")
