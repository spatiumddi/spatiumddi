"""appliance control-plane cluster columns (#272 Phase 7)

Adds the k3s control-plane cluster-membership columns to ``appliance``:

* ``cluster_role`` / ``desired_cluster_role`` — settled vs requested
  role in the k3s cluster (``primary`` seed / ``member`` / NULL).
* ``desired_k3s_server_url`` + ``desired_k3s_join_token_encrypted`` —
  join coordinates handed to a node being promoted to ``member``
  (token Fernet-encrypted at rest).
* ``k3s_join_token_encrypted`` — the seed's own join token, reported by
  the primary's supervisor (Fernet-encrypted), read by the promote
  endpoint to populate joiners.
* ``cluster_join_state`` / ``cluster_join_reason`` — supervisor-reported
  progress of an in-flight join/leave.

All nullable; no backfill — single-node installs leave them NULL and
behave identically.

Revision ID: c263ae1d381a
Revises: b9e1c43f7d28
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c263ae1d381a"
down_revision = "b9e1c43f7d28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appliance", sa.Column("cluster_role", sa.String(length=16), nullable=True))
    op.add_column(
        "appliance", sa.Column("desired_cluster_role", sa.String(length=16), nullable=True)
    )
    op.add_column("appliance", sa.Column("desired_k3s_server_url", sa.Text(), nullable=True))
    op.add_column(
        "appliance",
        sa.Column("desired_k3s_join_token_encrypted", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "appliance", sa.Column("k3s_join_token_encrypted", sa.LargeBinary(), nullable=True)
    )
    op.add_column("appliance", sa.Column("cluster_join_state", sa.String(length=16), nullable=True))
    op.add_column("appliance", sa.Column("cluster_join_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("appliance", "cluster_join_reason")
    op.drop_column("appliance", "cluster_join_state")
    op.drop_column("appliance", "k3s_join_token_encrypted")
    op.drop_column("appliance", "desired_k3s_join_token_encrypted")
    op.drop_column("appliance", "desired_k3s_server_url")
    op.drop_column("appliance", "desired_cluster_role")
    op.drop_column("appliance", "cluster_role")
