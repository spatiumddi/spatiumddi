"""Removable (USB) backup disks on the appliance (#989 item 3).

One JSONB column, NOT NULL, defaulting to the empty list. No backfill,
no data migration, nothing to derive: an install upgrades into this
feature with no removable disks configured, which is also what every
existing row means.

WHY THE DESIRED SET LIVES ON THE APPLIANCE ROW
----------------------------------------------
Every other host-config plane — snmp, ntp, lldp, syslog, ssh, resolver,
apt — renders from ``platform_settings``, because each describes the
FLEET. A USB disk is plugged into exactly one node, so a fleet-wide list
would ask every other node to mount a disk it cannot see, and each of
them would sit in ``waiting`` forever with nothing wrong.

WHY THE REPORTED STATE IS NOT A COLUMN
--------------------------------------
What is actually mounted rides inside ``cluster_health["removable"]``,
stored verbatim from the heartbeat (the #402 pattern). Desired and
actual disagreeing is the NORMAL condition here — a rotated off-site
disk is legitimately absent for days — so the two cannot be one field,
and a second column could only ever be a stale copy of what the node
already reports.

Revision ID: e3b9d7412c5a
Revises: a9f2c71e34b8
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "e3b9d7412c5a"
down_revision = "a9f2c71e34b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "desired_removable_mounts",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "desired_removable_mounts")
