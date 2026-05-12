"""Phase 8f-1/8f-2 — agent slot state + deployment_kind on dns_server / dhcp_server

Adds the columns needed for control-plane-driven fleet upgrade
orchestration (issue #138, Phase 8f). Two flavours of data:

* **Operator intent.** ``desired_appliance_version`` +
  ``desired_slot_image_url`` are set from the upcoming Fleet view —
  operator picks a release tag and the control plane stamps that
  desire onto every selected agent's row. The agent's existing
  ConfigBundle long-poll picks it up and fires the local
  ``spatiumddi-slot-upgrade.path`` trigger (same machinery the
  per-appliance OS Image card uses).

* **Agent reality.** ``current_slot`` / ``durable_default`` /
  ``is_trial_boot`` / ``last_upgrade_state`` / ``last_upgrade_state_at``
  / ``installed_appliance_version`` are agent-reported via the
  heartbeat path. The control plane just persists what the agent
  tells it. ``deployment_kind`` (``appliance`` / ``docker`` / ``k8s``
  / ``unknown``) lets the Fleet view branch UI affordances per row
  — appliance rows get an Upgrade button, docker/k8s rows get
  operator copy-paste commands instead.

All columns NULL-friendly so existing rows keep working with no
backfill — the agent fills them in on its next heartbeat. Same
column set on both ``dns_server`` and ``dhcp_server`` because the
two tables share the agent bookkeeping shape.

Revision ID: f8b1c20d3e72
Revises: e4a7f10b2c39
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f8b1c20d3e72"
down_revision: str | None = "e4a7f10b2c39"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_COLUMNS = [
    # Operator intent (Fleet view writes these; agent reads via ConfigBundle).
    ("desired_appliance_version", sa.String(64), True),
    ("desired_slot_image_url", sa.Text(), True),
    # Agent reality (heartbeat writes these; UI displays them).
    ("deployment_kind", sa.String(20), True),
    ("installed_appliance_version", sa.String(64), True),
    ("current_slot", sa.String(16), True),
    ("durable_default", sa.String(16), True),
    ("last_upgrade_state", sa.String(20), True),
]


def upgrade() -> None:
    for table in ("dns_server", "dhcp_server"):
        for name, type_, nullable in _NEW_COLUMNS:
            op.add_column(table, sa.Column(name, type_, nullable=nullable))
        # is_trial_boot defaults False because the trial flag is only
        # meaningful on appliance rows that have reported state; on
        # docker / k8s / pre-Phase-8f rows the column stays False.
        op.add_column(
            table,
            sa.Column(
                "is_trial_boot",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "last_upgrade_state_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    for table in ("dns_server", "dhcp_server"):
        op.drop_column(table, "last_upgrade_state_at")
        op.drop_column(table, "is_trial_boot")
        for name, _, _ in reversed(_NEW_COLUMNS):
            op.drop_column(table, name)
