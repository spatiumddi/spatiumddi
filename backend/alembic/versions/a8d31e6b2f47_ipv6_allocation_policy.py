"""Subnet.ipv6_allocation_policy for IPv6 auto-allocation.

Revision ID: a8d31e6b2f47
Revises: f7a2c4b91d35
Create Date: 2026-04-21 17:30:00

Controls how ``/next-address`` picks a host for IPv6 subnets. See
``Subnet.ipv6_allocation_policy`` on the model for the enum values.
Default "random" matches the most common deployment: /64 LAN with
CSPRNG-chosen host suffixes. Small subnets (>= /112) can opt into
"sequential"; "eui64" requires a MAC on the allocation request.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a8d31e6b2f47"
down_revision: str | None = "f7a2c4b91d35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "ipv6_allocation_policy",
            sa.String(length=20),
            nullable=False,
            server_default="random",
        ),
    )


def downgrade() -> None:
    op.drop_column("subnet", "ipv6_allocation_policy")
