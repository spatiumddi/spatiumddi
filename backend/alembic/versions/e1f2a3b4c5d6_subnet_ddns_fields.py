"""Subnet DDNS fields — enabled, hostname policy, domain override, TTL.

Revision ID: e1f2a3b4c5d6
Revises: d3f1ab7c8e02
Create Date: 2026-04-18 20:00:00

Adds the four DDNS control fields to ``subnet``. The existing
``DHCPScope.ddns_enabled`` + ``ddns_hostname_policy`` fields are left
alone — they drive Kea's native DDNS hook, which is a separate concern
from SpatiumDDI's reconciliation-layer DDNS (lease → A/PTR via the
same ``_sync_dns_record`` path static allocations use).

Defaults:
  * ``ddns_enabled = False`` — opt-in per subnet.
  * ``ddns_hostname_policy = 'client_or_generated'`` — most forgiving
    behaviour when DDNS is eventually turned on.
  * ``ddns_domain_override = NULL`` — publish into the subnet's primary
    forward zone unless overridden.
  * ``ddns_ttl = NULL`` — fall back to the zone's default TTL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d3f1ab7c8e02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "ddns_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "ddns_hostname_policy",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'client_or_generated'"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column("ddns_domain_override", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "subnet",
        sa.Column("ddns_ttl", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subnet", "ddns_ttl")
    op.drop_column("subnet", "ddns_domain_override")
    op.drop_column("subnet", "ddns_hostname_policy")
    op.drop_column("subnet", "ddns_enabled")
