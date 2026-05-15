"""Issue #170 Wave C1 — appliance row slot telemetry + desired_* fields.

Adds the appliance-host management surface to the ``appliance`` table
so the supervisor's heartbeat (new in C1) can persist + read the same
fields the legacy DNS / DHCP agents used to report on their own rows
in #138's Phase 8f-2.

New columns on ``appliance``:

* ``deployment_kind`` — ``appliance`` / ``docker`` / ``k8s`` /
  ``unknown``. Drives the Fleet UI's Upgrade affordance: appliance
  rows get an in-band slot upgrade; docker / k8s rows get the
  copy-paste compose / helm upgrade modal.
* ``installed_appliance_version`` — what's running on disk
  (``APPLIANCE_VERSION`` out of ``/etc/spatiumddi/appliance-release``).
* ``current_slot`` / ``durable_default`` / ``is_trial_boot`` —
  derived from /proc/cmdline + grubenv; lets the UI distinguish
  "running trial boot" from "durable default" without polling the
  appliance.
* ``last_upgrade_state`` / ``last_upgrade_state_at`` — surface for
  the .state sidecar the host-side ``spatium-upgrade-slot apply``
  runner maintains. One of ``ready`` / ``in-flight`` / ``done`` /
  ``failed``.
* ``snmpd_running`` — True/False/None from the snmpd sidecar
  (issue #153).
* ``ntp_sync_state`` — synchronized / unsynchronized / unknown from
  the chrony sidecar (issue #154).
* ``desired_appliance_version`` / ``desired_slot_image_url`` —
  operator's target version + raw.xz URL. Heartbeat returns these
  to the supervisor; supervisor writes the slot-upgrade trigger.
* ``reboot_requested`` / ``reboot_requested_at`` — operator's
  reboot signal. Supervisor's next heartbeat picks it up and writes
  the reboot trigger.

Revision ID: 79a2e409e774
Revises: c7e9b3a481f2
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "79a2e409e774"
down_revision: str | None = "c7e9b3a481f2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("deployment_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "installed_appliance_version", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("current_slot", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("durable_default", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "is_trial_boot",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("last_upgrade_state", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "last_upgrade_state_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("snmpd_running", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("ntp_sync_state", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "desired_appliance_version", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("desired_slot_image_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "reboot_requested",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "reboot_requested_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "reboot_requested_at")
    op.drop_column("appliance", "reboot_requested")
    op.drop_column("appliance", "desired_slot_image_url")
    op.drop_column("appliance", "desired_appliance_version")
    op.drop_column("appliance", "ntp_sync_state")
    op.drop_column("appliance", "snmpd_running")
    op.drop_column("appliance", "last_upgrade_state_at")
    op.drop_column("appliance", "last_upgrade_state")
    op.drop_column("appliance", "is_trial_boot")
    op.drop_column("appliance", "durable_default")
    op.drop_column("appliance", "current_slot")
    op.drop_column("appliance", "installed_appliance_version")
    op.drop_column("appliance", "deployment_kind")
