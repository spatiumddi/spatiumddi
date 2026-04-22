"""Kubernetes provenance FKs on IPAM + DNS rows — Phase 1b.

Revision ID: a917b4c9e251
Revises: f8c3d104e27a
Create Date: 2026-04-22 20:30:00

The reconciler needs a way to (1) identify "I synced this row from
cluster X" so deletion diffs are `WHERE kubernetes_cluster_id = ?`,
(2) cascade-delete mirrored rows when the cluster row is dropped, and
(3) signal to the UI "don't manually edit, it's k8s-managed".

Mirrors the existing ``auto_from_lease`` pattern on ``ip_address`` —
dedicated column per provenance, not a free-form ``source`` string.
``ON DELETE CASCADE`` so removing a cluster automatically sweeps its
mirrored rows; the reconciler never has to special-case cluster-gone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a917b4c9e251"
down_revision: str | None = "f8c3d104e27a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("ip_address", "ip_block", "dns_record"):
        op.add_column(
            table,
            sa.Column(
                "kubernetes_cluster_id",
                sa.UUID(),
                sa.ForeignKey("kubernetes_cluster.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_kubernetes_cluster_id",
            table,
            ["kubernetes_cluster_id"],
        )


def downgrade() -> None:
    for table in ("dns_record", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_kubernetes_cluster_id", table_name=table)
        op.drop_column(table, "kubernetes_cluster_id")
