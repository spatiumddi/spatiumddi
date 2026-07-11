"""Fortinet + Meraki firewall mirrors + shared block-list feeds (#606).

Phase 1 of the enterprise-firewall family (following the #605 Palo Alto shape):

* ``fortinet_firewall`` — one row per FortiGate VDOM (read-only mirror only;
  enforcement is the credential-free threat-feed path below).
* ``meraki_org`` — one row per Meraki organization (read-only mirror + opt-in
  per-client Blocked enforcement via the #601 block-sync framework).
* ``firewall_feed`` — SpatiumDDI-hosted token-scoped block-list feed that
  feed-polling firewalls (FortiGate External Threat Feed, Cisco SI, Check Point
  IOC) subscribe to (the "feed inversion").

Generalizes ``firewall_endpoint_object`` (the #605 mirror store) from a single
``panos_firewall_id`` owner to one-of-three vendor owners (PAN-OS / Fortinet /
Meraki), enforced by a ``num_nonnulls(...) = 1`` CHECK. Adds
``fortinet_firewall_id`` + ``meraki_org_id`` provenance FKs on ``ip_address`` /
``ip_block`` / ``subnet`` / ``nat_mapping`` (all ON DELETE CASCADE). Adds the
two ``platform_settings.integration_*_enabled`` toggles and seeds the three new
``feature_module`` rows.

Revision ID: a7c3e91f4d28
Revises: f4a1c9e072d5
Create Date: 2026-07-11 10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7c3e91f4d28"
down_revision: str | None = "f4a1c9e072d5"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def upgrade() -> None:
    # ── fortinet_firewall ────────────────────────────────────────────
    op.create_table(
        "fortinet_firewall",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="443"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ca_bundle_pem", sa.Text(), nullable=False, server_default=""),
        sa.Column("vdom", sa.String(length=64), nullable=False, server_default="root"),
        sa.Column(
            "api_token_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column(
            "ipam_space_id",
            sa.UUID(),
            sa.ForeignKey("ip_space.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dns_group_id",
            sa.UUID(),
            sa.ForeignKey("dns_server_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "mirror_address_objects", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("mirror_nat_rules", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "mirror_interfaces", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "mirror_dhcp_leases", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("sw_version", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("object_count", sa.Integer(), nullable=True),
        sa.Column("nat_rule_count", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fortinet_firewall_name", "fortinet_firewall", ["name"], unique=True)

    # ── meraki_org ───────────────────────────────────────────────────
    op.create_table(
        "meraki_org",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "base_url",
            sa.String(length=255),
            nullable=False,
            server_default="https://api.meraki.com/api/v1",
        ),
        sa.Column("org_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "api_key_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column(
            "network_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "ipam_space_id",
            sa.UUID(),
            sa.ForeignKey("ip_space.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dns_group_id",
            sa.UUID(),
            sa.ForeignKey("dns_server_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "mirror_policy_objects", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("mirror_vlans", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "mirror_dhcp_reservations", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("mirror_nat_rules", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mirror_clients", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column(
            "block_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "block_sync_api_key_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column(
            "block_policy_name", sa.String(length=127), nullable=False, server_default="Blocked"
        ),
        sa.Column("last_block_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_block_sync_error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("network_count", sa.Integer(), nullable=True),
        sa.Column("object_count", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meraki_org_name", "meraki_org", ["name"], unique=True)

    # ── firewall_feed ────────────────────────────────────────────────
    op.create_table(
        "firewall_feed",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="ip"),
        sa.Column(
            "token_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_ip", postgresql.INET(), nullable=True),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_firewall_feed_name", "firewall_feed", ["name"], unique=True)

    # ── generalize firewall_endpoint_object to 3 vendor owners ───────
    op.alter_column("firewall_endpoint_object", "panos_firewall_id", nullable=True)
    op.add_column(
        "firewall_endpoint_object",
        sa.Column(
            "fortinet_firewall_id",
            sa.UUID(),
            sa.ForeignKey("fortinet_firewall.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "firewall_endpoint_object",
        sa.Column(
            "meraki_org_id",
            sa.UUID(),
            sa.ForeignKey("meraki_org.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_firewall_object_fortinet_firewall_id",
        "firewall_endpoint_object",
        ["fortinet_firewall_id"],
    )
    op.create_index(
        "ix_firewall_object_meraki_org_id", "firewall_endpoint_object", ["meraki_org_id"]
    )
    op.create_unique_constraint(
        "uq_firewall_object_fortinet_name",
        "firewall_endpoint_object",
        ["fortinet_firewall_id", "name"],
    )
    op.create_unique_constraint(
        "uq_firewall_object_meraki_name",
        "firewall_endpoint_object",
        ["meraki_org_id", "name"],
    )
    op.create_check_constraint(
        "ck_firewall_object_one_owner",
        "firewall_endpoint_object",
        "num_nonnulls(panos_firewall_id, fortinet_firewall_id, meraki_org_id) = 1",
    )

    # ── vendor provenance FKs on IPAM rows ───────────────────────────
    for table in ("ip_address", "ip_block", "subnet", "nat_mapping"):
        op.add_column(
            table,
            sa.Column(
                "fortinet_firewall_id",
                sa.UUID(),
                sa.ForeignKey("fortinet_firewall.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "meraki_org_id",
                sa.UUID(),
                sa.ForeignKey("meraki_org.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_fortinet_firewall_id", table, ["fortinet_firewall_id"])
        op.create_index(f"ix_{table}_meraki_org_id", table, ["meraki_org_id"])

    # ── settings toggles ─────────────────────────────────────────────
    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_fortinet_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_meraki_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── feature modules ──────────────────────────────────────────────
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) VALUES "
            "('integrations.fortinet', false), "
            "('integrations.meraki', false), "
            "('security.firewall_feeds', true) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM feature_module WHERE id IN "
            "('integrations.fortinet', 'integrations.meraki', 'security.firewall_feeds')"
        )
    )
    op.drop_column("platform_settings", "integration_meraki_enabled")
    op.drop_column("platform_settings", "integration_fortinet_enabled")

    for table in ("nat_mapping", "subnet", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_meraki_org_id", table_name=table)
        op.drop_index(f"ix_{table}_fortinet_firewall_id", table_name=table)
        op.drop_column(table, "meraki_org_id")
        op.drop_column(table, "fortinet_firewall_id")

    op.drop_constraint(
        "ck_firewall_object_one_owner", "firewall_endpoint_object", type_="check"
    )
    op.drop_constraint(
        "uq_firewall_object_meraki_name", "firewall_endpoint_object", type_="unique"
    )
    op.drop_constraint(
        "uq_firewall_object_fortinet_name", "firewall_endpoint_object", type_="unique"
    )
    op.drop_index("ix_firewall_object_meraki_org_id", table_name="firewall_endpoint_object")
    op.drop_index(
        "ix_firewall_object_fortinet_firewall_id", table_name="firewall_endpoint_object"
    )
    op.drop_column("firewall_endpoint_object", "meraki_org_id")
    op.drop_column("firewall_endpoint_object", "fortinet_firewall_id")
    # Pre-#606 the panos owner was NOT NULL; restore it. Any rows with a NULL
    # owner (Fortinet/Meraki-owned) are gone with their columns above, but a
    # partially-migrated table could still hold NULLs — clear them first.
    op.execute(sa.text("DELETE FROM firewall_endpoint_object WHERE panos_firewall_id IS NULL"))
    op.alter_column("firewall_endpoint_object", "panos_firewall_id", nullable=False)

    op.drop_index("ix_firewall_feed_name", table_name="firewall_feed")
    op.drop_table("firewall_feed")
    op.drop_index("ix_meraki_org_name", table_name="meraki_org")
    op.drop_table("meraki_org")
    op.drop_index("ix_fortinet_firewall_name", table_name="fortinet_firewall")
    op.drop_table("fortinet_firewall")
