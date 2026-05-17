"""Appliance k3s version + Fernet-encrypted kubeconfig (#183 Phase 5).

Adds two columns to ``appliance``:

* ``k3s_version`` — upstream k3s release tag the slot was baked
  against, supplied by the supervisor on every heartbeat. Plain
  text — public information; shown on the Fleet UI row.
* ``kubeconfig_encrypted`` — admin kubeconfig YAML the supervisor
  ships, Fernet-encrypted at rest. Revealed via the Fleet UI's
  password-gated reveal endpoint (mirrors agent-bootstrap-keys).

Both columns are nullable so legacy compose appliances + pre-Phase-5
supervisors don't trip a NOT-NULL violation on the next heartbeat.

Revision ID: b3a82c0d4e95
Revises: c7e5d918a3f2
Create Date: 2026-05-16 03:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3a82c0d4e95"
down_revision = "c7e5d918a3f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("k3s_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("kubeconfig_encrypted", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "kubeconfig_encrypted")
    op.drop_column("appliance", "k3s_version")
