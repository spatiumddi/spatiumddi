"""dns dynamic-update ACLs: dns_zone.dynamic_update_enabled + dns_zone_update_acl + feature module

Revision ID: a3d9f1e64c72
Revises: d4a8e2b16f39
Create Date: 2026-07-19 12:00:00.000000

Issue #641 — operator-configurable dynamic-update (RFC 2136) ACL on DNS
zones. Adds:

* ``dns_zone.dynamic_update_enabled`` — boolean, default false + server
  default false so every existing zone stays exactly as it renders today
  (only the internal agent loopback grant). Flipping it on lets the zone
  accept DDNS updates from the clients enumerated in the new ACL table.
* ``dns_zone_update_acl`` — one row per authorized writer, ordered by
  ``seq`` (first-match). Each row identifies a writer by *either* a named
  TSIG key (``tsig_key_id`` FK → ``dns_tsig_key``) *or* a source
  address/prefix (``ip_cidr``); the CHECK constraint enforces exactly one
  of the two. ``action`` / ``name_scope`` / ``name_pattern`` /
  ``record_types`` are forward-compatible storage for the P2 BIND9
  ``update-policy`` fine-grained path — persisted now, rendered later.
* ``dns.dynamic_update_acl`` feature module — default-enabled (the surface
  exposes no secrets by itself; it references TSIG keys by name only).

The timestamp columns carry ``server_default=now()`` because a
hand-written ``create_table`` doesn't run the SQLAlchemy Python-side
defaults on a fresh-install migration path.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a3d9f1e64c72"
down_revision = "d4a8e2b16f39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_zone",
        sa.Column(
            "dynamic_update_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "dns_zone_update_acl",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("action", sa.String(length=10), nullable=False, server_default="grant"),
        sa.Column("match_kind", sa.String(length=10), nullable=False),
        sa.Column("tsig_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_cidr", sa.String(length=64), nullable=True),
        sa.Column("name_scope", sa.String(length=20), nullable=True),
        sa.Column("name_pattern", sa.String(length=255), nullable=True),
        sa.Column("record_types", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["zone_id"], ["dns_zone.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tsig_key_id"], ["dns_tsig_key.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "num_nonnulls(tsig_key_id, ip_cidr) = 1",
            name="ck_dns_zone_update_acl_one_identity",
        ),
    )
    op.create_index("ix_dns_zone_update_acl_zone_id", "dns_zone_update_acl", ["zone_id"])
    op.create_index("ix_dns_zone_update_acl_tsig_key_id", "dns_zone_update_acl", ["tsig_key_id"])
    op.create_index("ix_dns_zone_update_acl_zone_seq", "dns_zone_update_acl", ["zone_id", "seq"])

    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('dns.dynamic_update_acl', TRUE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'dns.dynamic_update_acl'"))
    op.drop_index("ix_dns_zone_update_acl_zone_seq", table_name="dns_zone_update_acl")
    op.drop_index("ix_dns_zone_update_acl_tsig_key_id", table_name="dns_zone_update_acl")
    op.drop_index("ix_dns_zone_update_acl_zone_id", table_name="dns_zone_update_acl")
    op.drop_table("dns_zone_update_acl")
    op.drop_column("dns_zone", "dynamic_update_enabled")
