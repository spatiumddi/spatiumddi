"""appliance.cluster_join_state_at — age of the in-flight cluster transition (#590)

No k3s cluster transition (promote / demote / dead-node eviction) has a
timeout: each converges only when a supervisor reports back. A node that
died mid-join, or a seed that can't reach the kubeapi, therefore pins the
row in ``joining`` / ``leaving`` / ``evicting`` indefinitely — which is how
a dead-node replace stranded a 3-node cluster at 2/3 for 50+ minutes.

The operator escape hatch (``POST …/clear-cluster-state``) needs to tell a
healthy multi-minute join apart from a wedged one, or an impatient click
seconds into a normal promote blanks the desired-state out from under the
running join and permanently strands ``cluster_role``. This column records
when ``cluster_join_state`` last CHANGED; both the Fleet UI affordance and
the endpoint's own guard key off its age.

Pure-additive nullable column. NULL means "never transitioned" (or a row
written before this migration), which the guard treats as "age unknown →
allow the clear" — the pre-#590 behaviour, and safe because such a row has
no in-flight transition to interrupt.

Revision ID: c7a3f1e28d94
Revises: b4f2a9c17e63
Create Date: 2026-07-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7a3f1e28d94"
down_revision = "b4f2a9c17e63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("cluster_join_state_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill in-flight rows so an appliance mid-transition across the
    # upgrade doesn't read as "age unknown" forever. ``now()`` is the most
    # conservative stamp available: it restarts the staleness clock rather
    # than instantly declaring a live join stuck.
    op.execute(
        """
        UPDATE appliance
           SET cluster_join_state_at = now()
         WHERE cluster_join_state IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("appliance", "cluster_join_state_at")
