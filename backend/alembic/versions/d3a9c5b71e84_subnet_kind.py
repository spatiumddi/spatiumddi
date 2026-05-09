"""subnet kind discriminator (unicast | multicast) — issue #126 Phase 2

Revision ID: d3a9c5b71e84
Revises: c8d2f47a90b3
Create Date: 2026-05-09 00:30:00

Adds ``subnet.kind`` so the IPAM tree + REST surface can fork
behaviour between unicast subnets (per-IP allocation) and
multicast subnets (groups instead of allocated endpoints).

Backfill walks every existing subnet and stamps ``kind=
'multicast'`` when the network CIDR sits inside the IANA
multicast ranges (``224.0.0.0/4`` IPv4 / ``ff00::/8`` IPv6).
Everything else stays ``unicast`` (the column default).

The check constraint pins values to the two known kinds; new
kinds (broadcast / anycast / etc) would land in their own
migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3a9c5b71e84"
down_revision: str | None = "c8d2f47a90b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'unicast'"),
        ),
    )
    op.create_check_constraint(
        "ck_subnet_kind",
        "subnet",
        "kind IN ('unicast','multicast')",
    )

    # Backfill — INET ``<<`` is "is contained in" for the
    # PostgreSQL network types. Cast the cidr column to inet
    # first since the comparison happens between an inet on the
    # left and an inet on the right.
    op.execute(
        sa.text(
            """
            UPDATE subnet
            SET kind = 'multicast'
            WHERE
                (family(network) = 4 AND host(network)::inet << inet '224.0.0.0/4')
                OR
                (family(network) = 6 AND host(network)::inet << inet 'ff00::/8')
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("ck_subnet_kind", "subnet", type_="check")
    op.drop_column("subnet", "kind")
