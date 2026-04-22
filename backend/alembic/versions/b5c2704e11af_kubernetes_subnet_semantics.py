"""Extend Kubernetes provenance to subnet + add mirror_pods toggle.

Revision ID: b5c2704e11af
Revises: a917b4c9e251
Create Date: 2026-04-22 21:05:00

Phase 1b evolved: the reconciler now auto-creates a ``Subnet`` per
pod/service CIDR so Service ClusterIPs + (optional) pod IPs can
actually land in IPAM. That needs:

* ``subnet.kubernetes_cluster_id`` — FK ON DELETE CASCADE so mirrored
  subnets get swept when the cluster row is removed.
* ``subnet.kubernetes_semantics`` — suppresses the LAN-specific
  network / broadcast / gateway placeholder rows in the IPAM edit
  path; pod and service CIDRs are routed overlays without those.
* ``kubernetes_cluster.mirror_pods`` — per-cluster opt-in to mirror
  every pod's IP into IPAM. Off by default because pods churn.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b5c2704e11af"
down_revision: str | None = "a917b4c9e251"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "kubernetes_cluster_id",
            sa.UUID(),
            sa.ForeignKey("kubernetes_cluster.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_subnet_kubernetes_cluster_id",
        "subnet",
        ["kubernetes_cluster_id"],
    )
    op.add_column(
        "subnet",
        sa.Column(
            "kubernetes_semantics",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "kubernetes_cluster",
        sa.Column(
            "mirror_pods",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("kubernetes_cluster", "mirror_pods")
    op.drop_column("subnet", "kubernetes_semantics")
    op.drop_index("ix_subnet_kubernetes_cluster_id", table_name="subnet")
    op.drop_column("subnet", "kubernetes_cluster_id")
