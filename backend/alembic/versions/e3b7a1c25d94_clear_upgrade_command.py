"""Host-side clear-upgrade command (#786)

Adds ``appliance.clear_upgrade_requested`` + ``…_at``.

"Clear failed upgrade" could not clear a failed upgrade. The endpoint
reset the four ``desired_slot_image_*`` columns, but the red failure card
renders from ``last_upgrade_state`` / ``last_upgrade_progress`` — and
those are re-published verbatim from host sidecar files under
``/var/lib/spatiumddi/release-state/`` on every heartbeat. Clearing them
in the DB would have been undone within 30 s, and rebooting never helped
because that directory is on the persistent ``/var`` a slot write never
touches.

So the clear has to be a command to the host, not a DB write. This flag
carries it: the supervisor removes any stranded ``slot-upgrade-pending``
trigger and resets its ``.state`` / ``.progress`` sidecars, then reports
``ready`` — at which point the cleared DB state stops being overwritten.

The flag is retired when the host ACKNOWLEDGES — the first heartbeat
reporting a state other than ``failed``, which is what a successful reset
produces. ``clear_upgrade_requested_at`` backs only a give-up timeout for
a host that can never comply, so the flag cannot pin that appliance's
heartbeat long-poll off forever.

Revision ID: e3b7a1c25d94
Revises: a4f1c93d7e28
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3b7a1c25d94"
down_revision = "a4f1c93d7e28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "clear_upgrade_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("clear_upgrade_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "clear_upgrade_requested_at")
    op.drop_column("appliance", "clear_upgrade_requested")
