"""platform_settings MetalLB BGP mode (issue #566 decision D1)

Adds the BGP-advertise (export path) knobs to the ``platform_settings``
singleton, layered on top of the existing L2/ARP MetalLB VIP columns
(``metallb_enabled`` / ``metallb_pool_addresses`` / ``control_plane_vip``,
added by ``e7a2c91d4f60``):

* ``metallb_bgp_enabled`` — master switch for BGP mode. Requires
  ``metallb_enabled=True`` (enforced at the API layer, not the DB).
* ``metallb_bgp_peers`` — JSONB list of ``{my_asn, peer_asn,
  peer_address, peer_port, hold_time}`` dicts, one BGPPeer CR per entry.
* ``metallb_bgp_advertisements`` — JSONB list of ``{ip_address_pools,
  communities, aggregation_length}`` dicts, one BGPAdvertisement CR per
  entry.

The seed supervisor reads these back on heartbeat and folds them into
the SAME ``apply_metallb_overrides`` HelmChartConfig write as the L2
pool (one combined ``valuesContent`` body — see
``agent/supervisor/spatium_supervisor/k8s_api.py``). Enabling this
activates the GPL-v2 FRRouting BGP daemon inside the cluster via
MetalLB's frr-k8s backend (see ``NOTICE``) — opt-in only, stays
dormant until an operator configures a peer.

Revision ID: 1440e72b9297
Revises: f7883fa6d413
Create Date: 2026-07-05 17:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "1440e72b9297"
down_revision = "f7883fa6d413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "metallb_bgp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "metallb_bgp_peers",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "metallb_bgp_advertisements",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "metallb_bgp_advertisements")
    op.drop_column("platform_settings", "metallb_bgp_peers")
    op.drop_column("platform_settings", "metallb_bgp_enabled")
