"""IPv6 Router Advertisement management + rogue-RA detection (#524)

Adds per-scope radvd settings on ``dhcp_scope`` (opt-in ``ra_enabled`` plus
lifetimes / prefix flags / interface / M-O override), the ``ra_observed_router``
+ ``ra_router_allowlist`` tables for the passive rogue-RA detector, and seeds
the default-enabled ``ipv6.router_advertisements`` feature module.

Revision ID: b7e4d1a92c30
Revises: a3d7f1c9e6b2
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7e4d1a92c30"
down_revision = "a3d7f1c9e6b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Per-scope RA settings (issue #524) ───────────────────────────
    op.add_column(
        "dhcp_scope",
        sa.Column("ra_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column("ra_mo_override", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "ra_router_lifetime", sa.Integer(), nullable=False, server_default=sa.text("1800")
        ),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column("ra_max_interval", sa.Integer(), nullable=False, server_default=sa.text("600")),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "ra_prefix_valid_lifetime",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("86400"),
        ),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "ra_prefix_preferred_lifetime",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("14400"),
        ),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "ra_prefix_on_link", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "ra_prefix_autonomous", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column("ra_interface", sa.String(length=64), nullable=False, server_default=""),
    )

    # ── 2. Rogue-RA observation store + allowlist ───────────────────────
    op.create_table(
        "ra_observed_router",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reported_by_server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_ip", postgresql.INET(), nullable=False),
        sa.Column("source_mac", postgresql.MACADDR(), nullable=True),
        sa.Column(
            "prefixes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("managed_flag", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("other_flag", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("router_lifetime", sa.Integer(), nullable=True),
        sa.Column("iface", sa.String(length=64), nullable=True),
        sa.Column("classification", sa.String(length=16), nullable=False, server_default="rogue"),
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
        sa.UniqueConstraint(
            "group_id", "source_ip", "source_mac", name="uq_ra_observed_group_ip_mac"
        ),
    )
    op.create_index("ix_ra_observed_router_group", "ra_observed_router", ["group_id"])
    op.create_index("ix_ra_observed_router_last_seen", "ra_observed_router", ["last_seen_at"])

    op.create_table(
        "ra_router_allowlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_ip", postgresql.INET(), nullable=True),
        sa.Column("source_mac", postgresql.MACADDR(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
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
    )
    op.create_index("ix_ra_router_allowlist_group", "ra_router_allowlist", ["group_id"])

    # ── 3. Feature module (default-enabled for discovery) ───────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('ipv6.router_advertisements', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'ipv6.router_advertisements'"))
    op.drop_index("ix_ra_router_allowlist_group", table_name="ra_router_allowlist")
    op.drop_table("ra_router_allowlist")
    op.drop_index("ix_ra_observed_router_last_seen", table_name="ra_observed_router")
    op.drop_index("ix_ra_observed_router_group", table_name="ra_observed_router")
    op.drop_table("ra_observed_router")
    op.drop_column("dhcp_scope", "ra_interface")
    op.drop_column("dhcp_scope", "ra_prefix_autonomous")
    op.drop_column("dhcp_scope", "ra_prefix_on_link")
    op.drop_column("dhcp_scope", "ra_prefix_preferred_lifetime")
    op.drop_column("dhcp_scope", "ra_prefix_valid_lifetime")
    op.drop_column("dhcp_scope", "ra_max_interval")
    op.drop_column("dhcp_scope", "ra_router_lifetime")
    op.drop_column("dhcp_scope", "ra_mo_override")
    op.drop_column("dhcp_scope", "ra_enabled")
