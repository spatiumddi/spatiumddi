"""Block / space DDNS inheritance fields.

Revision ID: e5c1b3d8f29a
Revises: d4e2f7a9b1c6
Create Date: 2026-04-21 13:30:00

Promotes the subnet-level DDNS fields up the IPAM chain:

  * ip_space: + 4 DDNS fields (enabled, hostname_policy, domain_override,
    ttl). Space has no ``ddns_inherit_settings`` — it's the root.
  * ip_block: + same 4 fields + ``ddns_inherit_settings`` (default True).
  * subnet:  + ``ddns_inherit_settings`` (default True).

Semantics mirror ``dns_inherit_settings`` — when ``ddns_inherit_settings``
is True, the effective DDNS config for a subnet is resolved by walking
up the block chain and falling back to the space. The default keeps
current behaviour: DDNS is disabled everywhere until someone opts in.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e5c1b3d8f29a"
down_revision: str | None = "d4e2f7a9b1c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ip_space ────────────────────────────────────────────────────────────
    op.add_column(
        "ip_space",
        sa.Column("ddns_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "ip_space",
        sa.Column(
            "ddns_hostname_policy",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'client_or_generated'"),
        ),
    )
    op.add_column(
        "ip_space",
        sa.Column("ddns_domain_override", sa.String(length=255), nullable=True),
    )
    op.add_column("ip_space", sa.Column("ddns_ttl", sa.Integer(), nullable=True))

    # ── ip_block ────────────────────────────────────────────────────────────
    op.add_column(
        "ip_block",
        sa.Column("ddns_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "ip_block",
        sa.Column(
            "ddns_hostname_policy",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'client_or_generated'"),
        ),
    )
    op.add_column(
        "ip_block",
        sa.Column("ddns_domain_override", sa.String(length=255), nullable=True),
    )
    op.add_column("ip_block", sa.Column("ddns_ttl", sa.Integer(), nullable=True))
    op.add_column(
        "ip_block",
        sa.Column(
            "ddns_inherit_settings",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # ── subnet ──────────────────────────────────────────────────────────────
    op.add_column(
        "subnet",
        sa.Column(
            "ddns_inherit_settings",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("subnet", "ddns_inherit_settings")

    op.drop_column("ip_block", "ddns_inherit_settings")
    op.drop_column("ip_block", "ddns_ttl")
    op.drop_column("ip_block", "ddns_domain_override")
    op.drop_column("ip_block", "ddns_hostname_policy")
    op.drop_column("ip_block", "ddns_enabled")

    op.drop_column("ip_space", "ddns_ttl")
    op.drop_column("ip_space", "ddns_domain_override")
    op.drop_column("ip_space", "ddns_hostname_policy")
    op.drop_column("ip_space", "ddns_enabled")
