"""dhcp lease cache (Kea cache-threshold / cache-max-age)

Issue #637 — Alpine 3.23 moves Kea 2.6.5 → 3.0.3, and Kea 3.0 turns lease
caching ON by default (``cache-threshold: 0.25``, since 2.7.8). When a client
re-requests a lease that still has more than 75% of its lifetime remaining,
Kea returns the SAME lease with an unchanged expiry and skips the lease-database
write entirely.

That silently changes SpatiumDDI's lease pipeline: the agent tails the memfile
CSV and POSTs lease-events, which drive DDNS and the IPAM lease mirror. Fewer
writes means fewer events, so a chatty client's DDNS record and IPAM
``last_seen`` would quietly go stale.

Rather than hardcoding either behaviour, the cache is exposed as a setting:

* ``dhcp_server_group.lease_cache_threshold`` — group-wide default, NOT NULL,
  **server_default 0.0 (disabled)**. Postgres backfills every existing row as
  part of the ADD COLUMN, so upgrading installs keep the exact pre-3.0
  write-through behaviour with no operator action.
* ``dhcp_server_group.lease_cache_max_age`` — nullable; NULL = no cap.
* ``dhcp_scope.lease_cache_threshold`` / ``lease_cache_max_age`` — nullable
  per-scope overrides; NULL = inherit the group value.

Revision ID: c7d3f9a15e28
Revises: b2e5d8a41c67
Create Date: 2026-07-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d3f9a15e28"
down_revision: str | None = "b2e5d8a41c67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with a server_default → Postgres backfills existing rows during
    # the ADD COLUMN, so no separate UPDATE is needed. 0.0 == caching disabled
    # == the Kea 2.6 behaviour these installs are upgrading from.
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "lease_cache_threshold",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )
    op.add_column(
        "dhcp_server_group",
        sa.Column("lease_cache_max_age", sa.Integer(), nullable=True),
    )

    # Per-scope overrides. NULL = inherit the group.
    op.add_column(
        "dhcp_scope",
        sa.Column("lease_cache_threshold", sa.Float(), nullable=True),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column("lease_cache_max_age", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dhcp_scope", "lease_cache_max_age")
    op.drop_column("dhcp_scope", "lease_cache_threshold")
    op.drop_column("dhcp_server_group", "lease_cache_max_age")
    op.drop_column("dhcp_server_group", "lease_cache_threshold")
