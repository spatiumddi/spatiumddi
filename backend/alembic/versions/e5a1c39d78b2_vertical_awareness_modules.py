"""AV / BACnet / OT vertical network-awareness modules (#543 — #540, #541, #542)

Adds three togglable, default-enabled Network feature modules and the
five tables behind them:

* ``network.av`` (#540) — ``av_flow_profile`` (1:1 AV descriptor on a
  multicast group) + ``av_reserved_range`` (operator-declared
  per-protocol multicast ranges).
* ``network.bacnet`` (#541) — ``bacnet_device``, whose
  ``uq_bacnet_device_instance`` constraint is the point of the whole
  module: BACnet device instance numbers must be unique across the
  entire internetwork, and enforcing that in the database is what
  turns a spreadsheet convention into a guarantee.
* ``network.ot`` (#542) — ``ot_device`` (1:1 OT descriptor on an IPAM
  address) + ``ot_zone`` (Purdue level + cell/area per subnet).

All three default-ENABLED, per non-negotiable #14's discovery
argument: operators can't turn off what they never knew existed, and
none of these surfaces touch secrets, make off-prem calls, or perform
broad-blast-radius writes. A plant that doesn't run AoIP or BACnet
turns them off in Settings → Features.

CHECK constraints are used where a value has a *specification-defined*
range that will never move (BACnet's 22-bit instance space, PTP's
single-octet domain, Purdue 0-5) — a bad value there is a data-integrity
bug, not a taxonomy that might grow. Protocol/role vocabularies stay
uncontrained frozensets validated at the API layer, matching
``multicast.py``'s stated rationale that adding a value later shouldn't
need a migration.

Revision ID: e5a1c39d78b2
Revises: b7f24d1e9a35
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5a1c39d78b2"
down_revision: str | None = "b7f24d1e9a35"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── #540 AV ────────────────────────────────────────────────────────
    op.create_table(
        "av_flow_profile",
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
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("av_protocol", sa.String(length=24), nullable=False),
        sa.Column(
            "av_flow_label", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("ptp_domain", sa.Integer(), nullable=True),
        sa.Column(
            "seen_via", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["group_id"], ["multicast_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", name="uq_av_flow_profile_group"),
        # IEEE 1588 domain numbers are a single octet.
        sa.CheckConstraint(
            "ptp_domain IS NULL OR (ptp_domain >= 0 AND ptp_domain <= 255)",
            name="ck_av_flow_profile_ptp_domain",
        ),
    )
    op.create_index("ix_av_flow_profile_av_protocol", "av_flow_profile", ["av_protocol"])
    op.create_index("ix_av_flow_profile_ptp_domain", "av_flow_profile", ["ptp_domain"])

    op.create_table(
        "av_reserved_range",
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
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cidr", postgresql.CIDR(), nullable=False),
        sa.Column("av_protocol", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("exclusive", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("vlan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["space_id"], ["ip_space.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vlan_id"], ["vlan.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "cidr", name="uq_av_reserved_range_space_cidr"),
        # Mirrors ck_multicast_group_address_class from #126 — a
        # reserved AV range only makes sense inside the multicast
        # space (224.0.0.0/4 v4, ff00::/8 v6).
        sa.CheckConstraint(
            "cidr <<= '224.0.0.0/4'::cidr OR cidr <<= 'ff00::/8'::cidr",
            name="ck_av_reserved_range_multicast",
        ),
    )
    op.create_index("ix_av_reserved_range_space_id", "av_reserved_range", ["space_id"])
    op.create_index("ix_av_reserved_range_av_protocol", "av_reserved_range", ["av_protocol"])
    op.create_index("ix_av_reserved_range_vlan_id", "av_reserved_range", ["vlan_id"])

    # ── #541 BACnet ────────────────────────────────────────────────────
    op.create_table(
        "bacnet_device",
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
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("device_instance", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=True),
        sa.Column(
            "vendor_name", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "device_name", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "model_name", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "firmware_rev", sa.String(length=128), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("location", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("max_apdu", sa.Integer(), nullable=True),
        sa.Column("segmentation_supported", sa.String(length=20), nullable=True),
        sa.Column("network_number", sa.Integer(), nullable=True),
        sa.Column("udp_port", sa.Integer(), nullable=False, server_default=sa.text("47808")),
        sa.Column("is_bbmd", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "is_foreign_device", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "bdt",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "fdt",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "seen_via", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["ip_address_id"], ["ip_address.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subnet_id"], ["subnet.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # THE constraint — internetwork-wide, per ASHRAE 135.
        sa.UniqueConstraint("device_instance", name="uq_bacnet_device_instance"),
        # 22-bit instance space; 4194303 is the reserved wildcard.
        sa.CheckConstraint(
            "device_instance >= 0 AND device_instance <= 4194302",
            name="ck_bacnet_device_instance_range",
        ),
        # 16-bit network numbers; 0 = local, 65535 = broadcast.
        sa.CheckConstraint(
            "network_number IS NULL OR (network_number >= 1 AND network_number <= 65534)",
            name="ck_bacnet_device_network_number",
        ),
        sa.CheckConstraint(
            "udp_port >= 1 AND udp_port <= 65535",
            name="ck_bacnet_device_udp_port",
        ),
    )
    op.create_index("ix_bacnet_device_ip_address_id", "bacnet_device", ["ip_address_id"])
    op.create_index("ix_bacnet_device_vendor_id", "bacnet_device", ["vendor_id"])
    op.create_index("ix_bacnet_device_subnet_bbmd", "bacnet_device", ["subnet_id", "is_bbmd"])
    # Partial: the BBMD topology list filters on is_bbmd alone and orders
    # by instance. BBMDs are a handful per estate, so this stays tiny and
    # saves a seq scan on every BACnet page load.
    op.create_index(
        "ix_bacnet_device_bbmd",
        "bacnet_device",
        ["subnet_id", "device_instance"],
        postgresql_where=sa.text("is_bbmd"),
    )

    # ── #542 OT ────────────────────────────────────────────────────────
    op.create_table(
        "ot_device",
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
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ot_protocol", sa.String(length=24), nullable=False),
        sa.Column("ot_role", sa.String(length=24), nullable=True),
        sa.Column(
            "profinet_device_name",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("ot_vendor", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "ot_product", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("ot_serial", sa.String(length=128), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "firmware_rev", sa.String(length=128), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("purdue_level", sa.Numeric(precision=2, scale=1), nullable=True),
        sa.Column("cell_area", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "seen_via", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["ip_address_id"], ["ip_address.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip_address_id", name="uq_ot_device_ip_address"),
        # Purdue model spans 0-5; 3.5 is the DMZ.
        sa.CheckConstraint(
            "purdue_level IS NULL OR (purdue_level >= 0 AND purdue_level <= 5)",
            name="ck_ot_device_purdue_level",
        ),
    )
    op.create_index("ix_ot_device_ot_protocol", "ot_device", ["ot_protocol"])
    op.create_index("ix_ot_device_ot_role", "ot_device", ["ot_role"])
    op.create_index("ix_ot_device_purdue_level", "ot_device", ["purdue_level"])

    op.create_table(
        "ot_zone",
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
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purdue_level", sa.Numeric(precision=2, scale=1), nullable=False),
        sa.Column("cell_area", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["subnet_id"], ["subnet.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subnet_id", name="uq_ot_zone_subnet"),
        sa.CheckConstraint(
            "purdue_level >= 0 AND purdue_level <= 5",
            name="ck_ot_zone_purdue_level",
        ),
    )
    op.create_index("ix_ot_zone_purdue_level", "ot_zone", ["purdue_level"])

    # ── feature_module seeds (non-negotiable #14) ──────────────────────
    op.execute(
        sa.text(
            """
            INSERT INTO feature_module (id, enabled)
            VALUES ('network.av', TRUE),
                   ('network.bacnet', TRUE),
                   ('network.ot', TRUE)
            ON CONFLICT (id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM feature_module "
            "WHERE id IN ('network.av', 'network.bacnet', 'network.ot')"
        )
    )

    op.drop_index("ix_ot_zone_purdue_level", table_name="ot_zone")
    op.drop_table("ot_zone")

    op.drop_index("ix_ot_device_purdue_level", table_name="ot_device")
    op.drop_index("ix_ot_device_ot_role", table_name="ot_device")
    op.drop_index("ix_ot_device_ot_protocol", table_name="ot_device")
    op.drop_table("ot_device")

    op.drop_index("ix_bacnet_device_bbmd", table_name="bacnet_device")
    op.drop_index("ix_bacnet_device_subnet_bbmd", table_name="bacnet_device")
    op.drop_index("ix_bacnet_device_vendor_id", table_name="bacnet_device")
    op.drop_index("ix_bacnet_device_ip_address_id", table_name="bacnet_device")
    op.drop_table("bacnet_device")

    op.drop_index("ix_av_reserved_range_vlan_id", table_name="av_reserved_range")
    op.drop_index("ix_av_reserved_range_av_protocol", table_name="av_reserved_range")
    op.drop_index("ix_av_reserved_range_space_id", table_name="av_reserved_range")
    op.drop_table("av_reserved_range")

    op.drop_index("ix_av_flow_profile_ptp_domain", table_name="av_flow_profile")
    op.drop_index("ix_av_flow_profile_av_protocol", table_name="av_flow_profile")
    op.drop_table("av_flow_profile")
