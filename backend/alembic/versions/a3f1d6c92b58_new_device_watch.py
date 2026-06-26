"""new-device watch: extend ip_mac_history + mac_allowlist (#459)

arpwatch-style new-device detection (issue #459). Rather than a parallel
sighting table, the existing ``ip_mac_history`` observation log (issue #369)
gains a classification layer:

* ``classification`` — new | acknowledged | known
* ``source`` — sweep | snmp | dhcp_lease | l2_sniff (which path observed it)
* ``is_randomized`` — locally-administered (privacy) MAC, skipped by the
  default alert so reconnecting phones don't storm
* ``acknowledged_at`` / ``acknowledged_by_user_id`` — operator dismissal trail

Plus a ``mac_allowlist`` table (MAC or OUI prefix) of trusted devices that
never alert — keyed on the MAC so it survives the cascade-delete of any IP it
was first seen on — and the default-off ``security.new_device_watch`` feature
module (non-negotiable #14).

All additive: every new column carries a ``server_default`` so existing rows
backfill in place and fresh-install + rolling-upgrade stay safe.

Revision ID: a3f1d6c92b58
Revises: 5443d2756647
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3f1d6c92b58"
down_revision: str | None = "5443d2756647"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── 1. Extend ip_mac_history with the classification layer ───────────
    op.add_column(
        "ip_mac_history",
        sa.Column(
            "classification",
            sa.String(length=16),
            server_default=sa.text("'new'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ip_mac_history",
        sa.Column(
            "source",
            sa.String(length=16),
            server_default=sa.text("'sweep'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ip_mac_history",
        sa.Column(
            "is_randomized",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "ip_mac_history",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ip_mac_history",
        sa.Column("acknowledged_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ip_mac_history_ack_user",
        "ip_mac_history",
        "user",
        ["acknowledged_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_ip_mac_history_classification", "ip_mac_history", ["classification"])

    # ── 2. mac_allowlist — trusted MACs / OUI prefixes ───────────────────
    op.create_table(
        "mac_allowlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=True),
        sa.Column("oui_prefix", sa.String(length=6), nullable=True),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("is_builtin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mac_address", name="uq_mac_allowlist_mac"),
        sa.CheckConstraint(
            "mac_address IS NOT NULL OR oui_prefix IS NOT NULL",
            name="ck_mac_allowlist_one_key",
        ),
    )
    op.create_index("ix_mac_allowlist_mac", "mac_allowlist", ["mac_address"])
    op.create_index("ix_mac_allowlist_oui_prefix", "mac_allowlist", ["oui_prefix"])

    # ── 3. feature_module seed (non-negotiable #14), default-OFF ─────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('security.new_device_watch', FALSE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'security.new_device_watch'"))
    op.drop_index("ix_mac_allowlist_oui_prefix", table_name="mac_allowlist")
    op.drop_index("ix_mac_allowlist_mac", table_name="mac_allowlist")
    op.drop_table("mac_allowlist")
    op.drop_index("ix_ip_mac_history_classification", table_name="ip_mac_history")
    op.drop_constraint("fk_ip_mac_history_ack_user", "ip_mac_history", type_="foreignkey")
    op.drop_column("ip_mac_history", "acknowledged_by_user_id")
    op.drop_column("ip_mac_history", "acknowledged_at")
    op.drop_column("ip_mac_history", "is_randomized")
    op.drop_column("ip_mac_history", "source")
    op.drop_column("ip_mac_history", "classification")
