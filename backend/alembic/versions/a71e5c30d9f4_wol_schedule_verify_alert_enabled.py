"""wol_schedule.verify_alert_enabled — per-schedule wake-failure alert mute (#596 Phase 2)

The new ``wol_wake_failed`` alert rule opens one event per schedule whose latest
finalised run left hosts unconfirmed. The rule's own ``enabled`` flag is the
master switch (seeded OFF, like ``rogue_dhcp``); this column is the per-schedule
mute, so one deliberately-noisy lab schedule doesn't force the operator to turn
the whole rule off.

Defaults to ``true``: once an operator enables the rule they mean it, and the
per-schedule opt-out is the exception rather than a second thing to remember to
switch on. Existing rows backfill to ``true`` via the server_default, which
changes nothing until the rule itself is enabled.

Pure-additive column add — safe under the expand/contract rolling upgrade
contract (an N-1 pod never reads it). Reuses the ``tools.wake_scheduler`` feature
module; the alert *rule* row is a global row seeded at startup, not module-gated,
matching ``rogue_dhcp`` / ``new_mac_seen``.

Revision ID: a71e5c30d9f4
Revises: c9a2f61e740b
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a71e5c30d9f4"
down_revision: str | None = "c9a2f61e740b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "wol_schedule",
        sa.Column(
            "verify_alert_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("wol_schedule", "verify_alert_enabled")
