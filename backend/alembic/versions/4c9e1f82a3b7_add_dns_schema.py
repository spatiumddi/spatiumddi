"""add dns schema

Revision ID: 4c9e1f82a3b7
Revises: 3b7f92c10d5e
Create Date: 2026-04-13 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4c9e1f82a3b7"
down_revision: Union[str, None] = "3b7f92c10d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dns_server_group",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("group_type", sa.String(50), nullable=False, server_default="internal"),
        sa.Column("default_view", sa.String(255), nullable=True),
        sa.Column("is_recursive", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dns_server_group_name", "dns_server_group", ["name"])

    op.create_table(
        "dns_server",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_server_group.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("driver", sa.String(50), nullable=False, server_default="bind9"),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer, nullable=False, server_default="53"),
        sa.Column("api_port", sa.Integer, nullable=True),
        sa.Column("api_key_encrypted", sa.Text, nullable=True),
        sa.Column("roles", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("group_id", "name", name="uq_dns_server_group_name"),
    )
    op.create_index("ix_dns_server_group_id", "dns_server", ["group_id"])

    op.create_table(
        "dns_server_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_server_group.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("forwarders", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("forward_policy", sa.String(20), nullable=False, server_default="first"),
        sa.Column("recursion_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("allow_recursion", postgresql.JSONB, nullable=False, server_default='["any"]'),
        sa.Column("dnssec_validation", sa.String(10), nullable=False, server_default="auto"),
        sa.Column("gss_tsig_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("gss_tsig_keytab_path", sa.String(500), nullable=True),
        sa.Column("gss_tsig_realm", sa.String(255), nullable=True),
        sa.Column("gss_tsig_principal", sa.String(500), nullable=True),
        sa.Column("notify_enabled", sa.String(20), nullable=False, server_default="yes"),
        sa.Column("also_notify", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("allow_notify", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("allow_query", postgresql.JSONB, nullable=False, server_default='["any"]'),
        sa.Column("allow_query_cache", postgresql.JSONB, nullable=False, server_default='["localhost", "localnets"]'),
        sa.Column("allow_transfer", postgresql.JSONB, nullable=False, server_default='["none"]'),
        sa.Column("blackhole", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dns_server_options_group_id", "dns_server_options", ["group_id"])

    op.create_table(
        "dns_trust_anchor",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("server_options_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_server_options.id", ondelete="CASCADE"), nullable=False),
        sa.Column("zone_name", sa.String(255), nullable=False),
        sa.Column("algorithm", sa.Integer, nullable=False),
        sa.Column("key_tag", sa.Integer, nullable=False),
        sa.Column("public_key", sa.Text, nullable=False),
        sa.Column("is_initial_key", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("added_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_dns_trust_anchor_options_id", "dns_trust_anchor", ["server_options_id"])

    op.create_table(
        "dns_acl",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_server_group.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("group_id", "name", name="uq_dns_acl_group_name"),
    )
    op.create_index("ix_dns_acl_group_id", "dns_acl", ["group_id"])
    op.create_index("ix_dns_acl_name", "dns_acl", ["name"])

    op.create_table(
        "dns_acl_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("acl_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_acl.id", ondelete="CASCADE"), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("negate", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("order", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_dns_acl_entry_acl_id", "dns_acl_entry", ["acl_id"])

    op.create_table(
        "dns_view",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_server_group.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("match_clients", postgresql.JSONB, nullable=False, server_default='["any"]'),
        sa.Column("match_destinations", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("recursion", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("group_id", "name", name="uq_dns_view_group_name"),
    )
    op.create_index("ix_dns_view_group_id", "dns_view", ["group_id"])

    op.create_table(
        "dns_zone",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_server_group.id", ondelete="CASCADE"), nullable=False),
        sa.Column("view_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_view.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("zone_type", sa.String(20), nullable=False, server_default="primary"),
        sa.Column("kind", sa.String(10), nullable=False, server_default="forward"),
        sa.Column("ttl", sa.Integer, nullable=False, server_default="3600"),
        sa.Column("refresh", sa.Integer, nullable=False, server_default="86400"),
        sa.Column("retry", sa.Integer, nullable=False, server_default="7200"),
        sa.Column("expire", sa.Integer, nullable=False, server_default="3600000"),
        sa.Column("minimum", sa.Integer, nullable=False, server_default="3600"),
        sa.Column("primary_ns", sa.String(255), nullable=False, server_default=""),
        sa.Column("admin_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("is_auto_generated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("linked_subnet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("subnet.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dnssec_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("last_serial", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allow_query", postgresql.JSONB, nullable=True),
        sa.Column("allow_transfer", postgresql.JSONB, nullable=True),
        sa.Column("also_notify", postgresql.JSONB, nullable=True),
        sa.Column("notify_enabled", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("group_id", "view_id", "name", name="uq_dns_zone_group_view_name"),
    )
    op.create_index("ix_dns_zone_group_id", "dns_zone", ["group_id"])
    op.create_index("ix_dns_zone_view_id", "dns_zone", ["view_id"])
    op.create_index("ix_dns_zone_name", "dns_zone", ["name"])

    op.create_table(
        "dns_record",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_zone.id", ondelete="CASCADE"), nullable=False),
        sa.Column("view_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dns_view.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("fqdn", sa.String(511), nullable=False, server_default=""),
        sa.Column("record_type", sa.String(10), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("ttl", sa.Integer, nullable=True),
        sa.Column("priority", sa.Integer, nullable=True),
        sa.Column("weight", sa.Integer, nullable=True),
        sa.Column("port", sa.Integer, nullable=True),
        sa.Column("auto_generated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ip_address.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dns_record_zone_id", "dns_record", ["zone_id"])
    op.create_index("ix_dns_record_zone_name", "dns_record", ["zone_id", "name"])
    op.create_index("ix_dns_record_fqdn", "dns_record", ["fqdn"])


def downgrade() -> None:
    op.drop_table("dns_record")
    op.drop_table("dns_zone")
    op.drop_table("dns_view")
    op.drop_table("dns_acl_entry")
    op.drop_table("dns_acl")
    op.drop_table("dns_trust_anchor")
    op.drop_table("dns_server_options")
    op.drop_table("dns_server")
    op.drop_table("dns_server_group")
