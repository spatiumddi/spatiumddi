"""bgp looking glass — collector + peers + learned RIB (issue #566)

Revision ID: cb279a6afd70
Revises: c9f2e1a4d7b6
Create Date: 2026-07-04 00:00:00.000000

Issue #566 — receive-only BGP Looking Glass. Three new tables + one
``feature_module`` seed row:

* ``looking_glass_collector`` — the GoBGP collector-daemon agent identity
  row (one per node), upserted by the register/heartbeat endpoints keyed on
  ``agent_id``. Shaped like ``dhcp_server`` / ``dns_server``.
* ``bgp_lg_peer`` — a configured receive-only BGP session. Carries the config
  the collector renders into the GoBGP neighbor block (``peer_asn`` /
  ``peer_address`` / ``address_families`` / ``max_prefixes`` / Fernet MD5)
  plus collector-reported runtime state (``session_state`` / ``prefixes_*``).
* ``bgp_lg_route`` — the learned RIB mirror, one row per
  ``(peer, prefix, next_hop)``. Absence-reconcile sets ``withdrawn_at`` rather
  than hard-deleting. ``matched_*_id`` FKs (SET NULL) link a learned route to
  the IPAM block / subnet / space / ASN / VRF it falls under (populated by a
  later phase; columns ship now so no follow-up migration is needed).

Seeds the ``network.looking_glass`` feature module (enabled — discovery
default per non-negotiable #14; the collector does nothing until a peer is
configured).

Additive only. Downgrade drops the three tables + the feature-module seed row
(new-table-only, so ``scripts/lint_migrations.py`` does not flag it).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "cb279a6afd70"
down_revision: Union[str, None] = "c9f2e1a4d7b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "looking_glass_collector",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=50),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=True),
        sa.Column(
            "agent_registered",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("agent_token_hash", sa.String(length=128), nullable=True),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("last_seen_ip", sa.String(length=45), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("appliance_id", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["appliance_id"], ["appliance.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_looking_glass_collector_agent_id"),
    )
    op.create_index(
        op.f("ix_looking_glass_collector_appliance_id"),
        "looking_glass_collector",
        ["appliance_id"],
    )
    op.create_index("ix_looking_glass_collector_name", "looking_glass_collector", ["name"])

    op.create_table(
        "bgp_lg_peer",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("collector_id", sa.UUID(), nullable=False),
        sa.Column("local_asn", sa.BigInteger(), nullable=False),
        sa.Column("peer_asn", sa.BigInteger(), nullable=False),
        sa.Column("peer_address", postgresql.INET(), nullable=False),
        sa.Column("matched_asn_id", sa.UUID(), nullable=True),
        sa.Column("peer_router_id", sa.UUID(), nullable=True),
        sa.Column(
            "address_families",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text('\'["ipv4-unicast"]\'::jsonb'),
            nullable=False,
        ),
        sa.Column("md5_password_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column(
            "max_prefixes",
            sa.Integer(),
            server_default=sa.text("10000"),
            nullable=False,
        ),
        sa.Column(
            "import_filter",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text('\'{"mode": "accept_all"}\'::jsonb'),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "session_state",
            sa.String(length=24),
            server_default=sa.text("'idle'"),
            nullable=False,
        ),
        sa.Column("uptime_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "prefixes_received", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "prefixes_accepted", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_state_change", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_flap_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rpki_invalid_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("down_since", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["collector_id"], ["looking_glass_collector.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["matched_asn_id"], ["asn.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["peer_router_id"], ["network_device.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collector_id", "peer_address", name="uq_bgp_lg_peer_addr"),
    )
    op.create_index("ix_bgp_lg_peer_collector", "bgp_lg_peer", ["collector_id"])
    op.create_index("ix_bgp_lg_peer_enabled", "bgp_lg_peer", ["enabled"])
    op.create_index(op.f("ix_bgp_lg_peer_matched_asn_id"), "bgp_lg_peer", ["matched_asn_id"])
    op.create_index(op.f("ix_bgp_lg_peer_peer_router_id"), "bgp_lg_peer", ["peer_router_id"])

    op.create_table(
        "bgp_lg_route",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("peer_id", sa.UUID(), nullable=False),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("origin_asn", sa.BigInteger(), nullable=True),
        sa.Column(
            "as_path",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("next_hop", postgresql.INET(), nullable=False),
        sa.Column("local_pref", sa.Integer(), nullable=True),
        sa.Column("med", sa.Integer(), nullable=True),
        sa.Column(
            "communities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "large_communities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "ext_communities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "rpki_status",
            sa.String(length=12),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("is_best", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("matched_block_id", sa.UUID(), nullable=True),
        sa.Column("matched_subnet_id", sa.UUID(), nullable=True),
        sa.Column("matched_space_id", sa.UUID(), nullable=True),
        sa.Column("matched_asn_id", sa.UUID(), nullable=True),
        sa.Column("matched_vrf_id", sa.UUID(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flap_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["matched_asn_id"], ["asn.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matched_block_id"], ["ip_block.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matched_space_id"], ["ip_space.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matched_subnet_id"], ["subnet.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matched_vrf_id"], ["vrf.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["peer_id"], ["bgp_lg_peer.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("peer_id", "prefix", "next_hop", name="uq_bgp_lg_route"),
    )
    op.create_index(
        "ix_bgp_lg_route_active",
        "bgp_lg_route",
        ["peer_id", "prefix"],
        postgresql_where=sa.text("withdrawn_at IS NULL"),
    )
    op.create_index("ix_bgp_lg_route_matched_asn", "bgp_lg_route", ["matched_asn_id"])
    op.create_index("ix_bgp_lg_route_matched_block", "bgp_lg_route", ["matched_block_id"])
    op.create_index("ix_bgp_lg_route_matched_space", "bgp_lg_route", ["matched_space_id"])
    op.create_index("ix_bgp_lg_route_matched_subnet", "bgp_lg_route", ["matched_subnet_id"])
    op.create_index("ix_bgp_lg_route_matched_vrf", "bgp_lg_route", ["matched_vrf_id"])
    op.create_index("ix_bgp_lg_route_origin_asn", "bgp_lg_route", ["origin_asn"])
    op.create_index("ix_bgp_lg_route_peer", "bgp_lg_route", ["peer_id"])
    op.create_index("ix_bgp_lg_route_prefix", "bgp_lg_route", ["prefix"])
    op.create_index("ix_bgp_lg_route_rpki_status", "bgp_lg_route", ["rpki_status"])

    # Feature-module seed (non-negotiable #14). Enabled = discovery default.
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) "
            "VALUES ('network.looking_glass', TRUE) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM feature_module WHERE id = 'network.looking_glass'")
    )

    op.drop_index("ix_bgp_lg_route_rpki_status", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_prefix", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_peer", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_origin_asn", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_matched_vrf", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_matched_subnet", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_matched_space", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_matched_block", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_matched_asn", table_name="bgp_lg_route")
    op.drop_index("ix_bgp_lg_route_active", table_name="bgp_lg_route")
    op.drop_table("bgp_lg_route")

    op.drop_index(op.f("ix_bgp_lg_peer_peer_router_id"), table_name="bgp_lg_peer")
    op.drop_index(op.f("ix_bgp_lg_peer_matched_asn_id"), table_name="bgp_lg_peer")
    op.drop_index("ix_bgp_lg_peer_enabled", table_name="bgp_lg_peer")
    op.drop_index("ix_bgp_lg_peer_collector", table_name="bgp_lg_peer")
    op.drop_table("bgp_lg_peer")

    op.drop_index("ix_looking_glass_collector_name", table_name="looking_glass_collector")
    op.drop_index(
        op.f("ix_looking_glass_collector_appliance_id"),
        table_name="looking_glass_collector",
    )
    op.drop_table("looking_glass_collector")
