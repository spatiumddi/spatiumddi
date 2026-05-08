"""multicast group registry — Phase 1 (issue #126)

Revision ID: f1e7a3c92b40
Revises: e7f94b21c8d5
Create Date: 2026-05-08 00:00:00

Lands the multicast group registry tables — ``multicast_group``,
``multicast_group_port``, ``multicast_membership`` — plus the
matching ``feature_module`` row. Default-enabled per CLAUDE.md
non-negotiable #14: operators discover features via the sidebar
and turn off what they don't use.

Three CHECK constraints encode the invariants the model layer
relies on:

* ``ck_multicast_group_address_class`` — the ``address`` INET
  must be inside ``224.0.0.0/4`` IPv4 or ``ff00::/8`` IPv6.
  Defends against a misconfigured client that posts a unicast IP.
* ``ck_multicast_group_port_range`` — ``port_end IS NULL OR
  port_end >= port_start``. NULL means "single port".
* ``ck_multicast_group_port_bounds`` — both ports in the
  IANA-blessed 0-65535 range.

The membership table carries a unique ``(group_id, ip_address_id,
role)`` triple so concurrent IGMP-snoop populators (Phase 3)
can't race-create dup rows; the same IP can hold multiple roles
on a single group (RP + producer is real), so role is part of
the key.

The ``domain_id`` column on ``multicast_group`` is a plain
nullable UUID with no FK — Phase 2 lands ``multicast_domain``
and adds the FK there. Putting the column in now means Phase 2
doesn't need a backfill migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

from alembic import op

revision: str = "f1e7a3c92b40"
down_revision: str | None = "e7f94b21c8d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "multicast_group",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ip_space.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("address", INET(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("application", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("rtp_payload_type", sa.Integer(), nullable=True),
        sa.Column("bandwidth_mbps_estimate", sa.Numeric(10, 3), nullable=True),
        sa.Column(
            "vlan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vlan.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "customer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("customer.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "service_id",
            UUID(as_uuid=True),
            sa.ForeignKey("network_service.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # No FK — multicast_domain table lands in Phase 2. Plain
        # nullable UUID column so Phase 2 just attaches the FK
        # without needing to backfill.
        sa.Column("domain_id", UUID(as_uuid=True), nullable=True),
        sa.Column("tags", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "custom_fields", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
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
        sa.CheckConstraint(
            "(family(address) = 4 AND address << inet '224.0.0.0/4') "
            "OR (family(address) = 6 AND address << inet 'ff00::/8')",
            name="ck_multicast_group_address_class",
        ),
    )
    op.create_index("ix_multicast_group_space_id", "multicast_group", ["space_id"])
    op.create_index("ix_multicast_group_vlan_id", "multicast_group", ["vlan_id"])
    op.create_index("ix_multicast_group_customer_id", "multicast_group", ["customer_id"])
    op.create_index("ix_multicast_group_service_id", "multicast_group", ["service_id"])
    op.create_index("ix_multicast_group_domain_id", "multicast_group", ["domain_id"])
    op.create_index("ix_multicast_group_address", "multicast_group", ["address"])

    op.create_table(
        "multicast_group_port",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("multicast_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("port_start", sa.Integer(), nullable=False),
        sa.Column("port_end", sa.Integer(), nullable=True),
        sa.Column(
            "transport", sa.String(length=8), nullable=False, server_default=sa.text("'udp'")
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.CheckConstraint(
            "port_start >= 0 AND port_start <= 65535 "
            "AND (port_end IS NULL OR (port_end >= 0 AND port_end <= 65535))",
            name="ck_multicast_group_port_bounds",
        ),
        sa.CheckConstraint(
            "port_end IS NULL OR port_end >= port_start",
            name="ck_multicast_group_port_range",
        ),
    )
    op.create_index(
        "ix_multicast_group_port_group_id", "multicast_group_port", ["group_id"]
    )

    op.create_table(
        "multicast_membership",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("multicast_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ip_address_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ip_address.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'consumer'"),
        ),
        sa.Column(
            "seen_via",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.UniqueConstraint(
            "group_id",
            "ip_address_id",
            "role",
            name="uq_multicast_membership_triplet",
        ),
    )
    op.create_index(
        "ix_multicast_membership_group_id", "multicast_membership", ["group_id"]
    )
    op.create_index(
        "ix_multicast_membership_ip_address_id",
        "multicast_membership",
        ["ip_address_id"],
    )

    # Seed the feature_module row. Default-enabled per CLAUDE.md
    # non-negotiable #14 (operators discover via the sidebar).
    # Idempotent so a re-stamp of head doesn't fail.
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) VALUES (:id, :enabled) "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(id="network.multicast", enabled=True)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM feature_module WHERE id = :id").bindparams(
            id="network.multicast"
        )
    )

    op.drop_index("ix_multicast_membership_ip_address_id", table_name="multicast_membership")
    op.drop_index("ix_multicast_membership_group_id", table_name="multicast_membership")
    op.drop_table("multicast_membership")

    op.drop_index("ix_multicast_group_port_group_id", table_name="multicast_group_port")
    op.drop_table("multicast_group_port")

    op.drop_index("ix_multicast_group_address", table_name="multicast_group")
    op.drop_index("ix_multicast_group_domain_id", table_name="multicast_group")
    op.drop_index("ix_multicast_group_service_id", table_name="multicast_group")
    op.drop_index("ix_multicast_group_customer_id", table_name="multicast_group")
    op.drop_index("ix_multicast_group_vlan_id", table_name="multicast_group")
    op.drop_index("ix_multicast_group_space_id", table_name="multicast_group")
    op.drop_table("multicast_group")
