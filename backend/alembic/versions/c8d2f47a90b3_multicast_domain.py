"""multicast PIM domain registry — Phase 2 Wave 1 (issue #126)

Revision ID: c8d2f47a90b3
Revises: f1e7a3c92b40
Create Date: 2026-05-09 00:00:00

Lands the ``multicast_domain`` table — PIM mode + RP + VRF
binding — and promotes the placeholder ``multicast_group.
domain_id`` UUID column (which Phase 1 left as a plain nullable
UUID) into a real FK with ``ON DELETE SET NULL``.

PIM modes carried server-side: ``sparse | dense | ssm | bidir |
none``. ``none`` is the manual / static-RP case (real in some
pro-audio deployments where the operator pins receivers
manually rather than running PIM).

The MSDP peering table is intentionally deferred to Phase 2.5 /
3 alongside the multi-domain UX — until SNMP-driven population
exists, operators don't have a workflow that needs MSDP
modelled at this layer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "c8d2f47a90b3"
down_revision: str | None = "f1e7a3c92b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "multicast_domain",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "pim_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'sparse'"),
        ),
        sa.Column(
            "vrf_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vrf.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "rendezvous_point_device_id",
            UUID(as_uuid=True),
            sa.ForeignKey("network_device.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rendezvous_point_address", sa.String(length=45), nullable=True),
        sa.Column("ssm_range", sa.String(length=45), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("tags", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
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
            "pim_mode IN ('sparse','dense','ssm','bidir','none')",
            name="ck_multicast_domain_pim_mode",
        ),
    )
    op.create_index("ix_multicast_domain_vrf_id", "multicast_domain", ["vrf_id"])
    op.create_index(
        "ix_multicast_domain_rp_device",
        "multicast_domain",
        ["rendezvous_point_device_id"],
    )

    # Promote the placeholder ``multicast_group.domain_id`` column
    # to a real FK. Phase 1 left it as a plain UUID with no
    # referential integrity (since the target table didn't exist
    # yet). The migration is safe: any non-NULL values written by
    # Phase 1 deployments are operator-typed UUIDs that should
    # exactly match a multicast_domain.id once the operator
    # creates the matching domain — but we can't backfill them
    # automatically. NULL values stay NULL.
    op.create_foreign_key(
        "fk_multicast_group_domain_id",
        "multicast_group",
        "multicast_domain",
        ["domain_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_multicast_group_domain_id",
        "multicast_group",
        type_="foreignkey",
    )

    op.drop_index("ix_multicast_domain_rp_device", table_name="multicast_domain")
    op.drop_index("ix_multicast_domain_vrf_id", table_name="multicast_domain")
    op.drop_table("multicast_domain")
