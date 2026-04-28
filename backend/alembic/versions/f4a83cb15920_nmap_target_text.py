"""widen nmap_scan.target_ip to TEXT (accept hostnames)

Revision ID: f4a83cb15920
Revises: d2f7a91e4c8b
Create Date: 2026-04-28 22:00:00.000000

The ``target_ip`` column is keeping its name for FK/audit continuity
but the value can now be a hostname / FQDN as well as an IP literal —
nmap accepts both, and operators routinely want to scan
``router1.lan`` without first resolving it. Postgres ``INET`` rejects
non-IP input, so the column is widened to ``VARCHAR(255)`` (DNS hard
upper bound). The companion index is retained — ix-scans on textual
prefixes still work for the ``target_ip=`` query filter.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f4a83cb15920"
down_revision: str | None = "d2f7a91e4c8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "nmap_scan",
        "target_ip",
        existing_type=postgresql.INET(),
        type_=sa.String(length=255),
        existing_nullable=False,
        postgresql_using="target_ip::text",
    )


def downgrade() -> None:
    # Best-effort downgrade — only IP-bearing rows can survive the
    # cast back to INET. Operator-stored hostnames will fail the cast,
    # so they need to be deleted first if a downgrade is required.
    op.alter_column(
        "nmap_scan",
        "target_ip",
        existing_type=sa.String(length=255),
        type_=postgresql.INET(),
        existing_nullable=False,
        postgresql_using="target_ip::inet",
    )
