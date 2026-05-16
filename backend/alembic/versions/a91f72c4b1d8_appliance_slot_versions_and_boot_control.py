"""Per-slot installed-version visibility + remote boot control.

Adds four columns to the ``appliance`` table:

* ``slot_a_version`` / ``slot_b_version`` — per-slot installed
  ``APPLIANCE_VERSION`` reported by the supervisor on every heartbeat.
  Sourced from the ``/var/lib/spatiumddi/release-state/slot-versions
  .json`` sidecar maintained by ``spatium-upgrade-slot sync-versions``
  (firstboot writes it at every boot + after every apply). Lets the
  Fleet drilldown render two side-by-side per-slot version cards so
  operators see which release is on each slot without SSH'ing in.

* ``desired_next_boot_slot`` — operator's one-shot "boot this slot
  next" intent. Supervisor reads it on the next heartbeat + writes
  the ``slot-set-next-boot-pending`` trigger; host runner invokes
  ``spatium-upgrade-slot set-next-boot <slot>`` (grub-reboot, auto-
  reverts on the boot AFTER the trial).

* ``desired_default_slot`` — operator's *durable* "make this slot the
  default" intent. Supervisor writes the ``slot-set-default-pending``
  trigger; runner invokes ``spatium-upgrade-slot set-default <slot>``
  (grub-set-default, survives subsequent reboots). Use to commit a
  trial boot once /health/live has proved it good, or to durably
  revert.

Both ``desired_*`` columns auto-clear in the heartbeat handler once
the supervisor's reported state catches up (``current_slot`` /
``durable_default`` matches what was asked for).

Revision ID: a91f72c4b1d8
Revises: e8c3f1b9a724
Create Date: 2026-05-15 22:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a91f72c4b1d8"
down_revision = "e8c3f1b9a724"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("slot_a_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("slot_b_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("desired_next_boot_slot", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("desired_default_slot", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "desired_default_slot")
    op.drop_column("appliance", "desired_next_boot_slot")
    op.drop_column("appliance", "slot_b_version")
    op.drop_column("appliance", "slot_a_version")
