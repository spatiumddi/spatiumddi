"""Issue #285 Phase 1 — appliance fleet-firewall prerequisites.

Adds the telemetry columns the supervisor reports so the (future)
server-side firewall compiler can scope the k3s data-plane + apiserver
rules correctly BEFORE the LAN-wide base nftables accept is removed:

* ``node_ips``           — every k3s-registered InternalIP (both
                           families) for family-split /32 + /128 peer
                           scoping. NOT NULL, defaults to ``[]``.
* ``pod_cidr`` /
  ``service_cidr``       — operator-chosen k3s CIDRs (#302), read from
                           the ``spatium-cidrs.yaml`` drop-in. The 6443
                           rule must accept from these.
* ``dataplane_backend``  — ``vxlan`` / ``wireguard-native`` / … —
                           selects the inter-node data-plane port that
                           must be peer-opened on every pod-running node.
* ``base_conf_marker``   — sha256 of the live ``/etc/nftables.conf``.
* ``base_lanwide_k3s``   — whether the legacy LAN-wide ``k3s-ha`` accept
                           is still present (the half-A/B-upgrade signal).

All columns are purely additive telemetry — nothing here changes a
rendered or live firewall. The nullable columns follow the heartbeat's
"only update when not None" persistence semantics so a legacy / pre-#285
supervisor never blanks them.

Revision ID: d7e2a4f9c1b3
Revises: c1f4a8e3b29d
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "d7e2a4f9c1b3"
down_revision: str | None = "c1f4a8e3b29d"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "node_ips",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("appliance", sa.Column("pod_cidr", sa.String(length=128), nullable=True))
    op.add_column("appliance", sa.Column("service_cidr", sa.String(length=128), nullable=True))
    op.add_column("appliance", sa.Column("dataplane_backend", sa.String(length=32), nullable=True))
    op.add_column("appliance", sa.Column("base_conf_marker", sa.String(length=64), nullable=True))
    op.add_column("appliance", sa.Column("base_lanwide_k3s", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("appliance", "base_lanwide_k3s")
    op.drop_column("appliance", "base_conf_marker")
    op.drop_column("appliance", "dataplane_backend")
    op.drop_column("appliance", "service_cidr")
    op.drop_column("appliance", "pod_cidr")
    op.drop_column("appliance", "node_ips")
