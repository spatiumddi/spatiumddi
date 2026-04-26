"""IP role + reservation TTL + MAC history.

Revision ID: f1c9a4d2b8e6
Revises: e6f12b9a3c84
Create Date: 2026-04-26 12:00:00

Adds:
  * ``ip_address.role`` — nullable VARCHAR(20). Allowed values
    enforced at the API layer (see ``IP_ROLES``). Roles in
    ``IP_ROLES_SHARED`` (anycast / vip / vrrp) bypass MAC
    collision warnings on create / update.
  * ``ip_address.reserved_until`` — nullable TIMESTAMPTZ. Only
    meaningful when ``status='reserved'``. The
    ``sweep_expired_reservations`` Celery beat task flips rows
    whose TTL has passed back to ``available`` and clears the
    column.
  * ``platform_settings.reservation_sweep_enabled`` — boolean,
    default true. Gates the sweep task so operators can opt out
    without removing the beat schedule.
  * ``ip_mac_history`` table — per-IP append-only-ish MAC
    observation log. The IPAM update handler upserts
    ``(ip_address_id, mac_address)`` on every write, bumping
    ``last_seen``. Implicit history is the set of distinct
    rows; rotating MACs leaves prior rows intact with their
    own ``last_seen`` timestamps. Cascade-deletes with the
    parent IP.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1c9a4d2b8e6"
down_revision: str | None = "e6f12b9a3c84"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ip_address",
        sa.Column("role", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "ip_address",
        sa.Column("reserved_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "reservation_sweep_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    op.create_table(
        "ip_mac_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "ip_address_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ip_address.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=False),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "ip_address_id", "mac_address", name="uq_ip_mac_history_ip_mac"
        ),
    )
    op.create_index(
        "ix_ip_mac_history_ip_address_id",
        "ip_mac_history",
        ["ip_address_id"],
    )
    op.create_index(
        "ix_ip_mac_history_last_seen",
        "ip_mac_history",
        ["last_seen"],
    )


def downgrade() -> None:
    op.drop_index("ix_ip_mac_history_last_seen", table_name="ip_mac_history")
    op.drop_index("ix_ip_mac_history_ip_address_id", table_name="ip_mac_history")
    op.drop_table("ip_mac_history")
    op.drop_column("platform_settings", "reservation_sweep_enabled")
    op.drop_column("ip_address", "reserved_until")
    op.drop_column("ip_address", "role")
