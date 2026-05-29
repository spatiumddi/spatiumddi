"""DNSSEC — BIND9 dnssec-policy + key state (#49)

Revision ID: f2b6d4a91c37
Revises: e4c1a8f63b29
Create Date: 2026-05-29 21:30:00.000000

Adds the BIND9 DNSSEC tables (issue #49):

* ``dnssec_policy`` — reusable ``dnssec-policy`` definitions (algorithm /
  NSEC3 params / KSK+ZSK lifetimes). Seeds one built-in ``default`` row.
* ``dnssec_key`` — public per-zone key state reported by the agent (key
  tag / type / state / DS rrset). No private material.
* ``dns_zone.dnssec_policy_id`` — FK (SET NULL) pinning a zone's policy.

Additive — all server-defaulted; the seed is idempotent on re-run.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2b6d4a91c37"
down_revision: Union[str, None] = "e4c1a8f63b29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Stable UUID for the seeded built-in "default" policy so re-runs + cross-
# install backups stay consistent.
_DEFAULT_POLICY_ID = "0000002d-0049-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "dnssec_policy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "algorithm", sa.String(length=20), nullable=False, server_default="ecdsap256sha256"
        ),
        sa.Column("ksk_lifetime_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("zsk_lifetime_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("nsec3", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("nsec3_iterations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nsec3_salt_length", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nsec3_optout", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_dnssec_policy_name"),
    )
    op.create_index("ix_dnssec_policy_name", "dnssec_policy", ["name"])

    op.create_table(
        "dnssec_key",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_tag", sa.Integer(), nullable=False),
        sa.Column("key_type", sa.String(length=4), nullable=False),
        sa.Column("algorithm", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("ds_records", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("timing", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["zone_id"], ["dns_zone.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dnssec_key_zone", "dnssec_key", ["zone_id"])

    op.add_column(
        "dns_zone",
        sa.Column("dnssec_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dns_zone_dnssec_policy",
        "dns_zone",
        "dnssec_policy",
        ["dnssec_policy_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Seed the built-in "default" policy (idempotent). Cast the bound id to
    # uuid — the column is UUID, and a plain bind param arrives as VARCHAR.
    op.execute(
        sa.text(
            "INSERT INTO dnssec_policy "
            "(id, name, description, is_builtin, algorithm, "
            "ksk_lifetime_days, zsk_lifetime_days, "
            "nsec3, nsec3_iterations, nsec3_salt_length, nsec3_optout) "
            "VALUES (CAST(:id AS uuid), 'default', "
            "'BIND9 built-in default policy: ECDSAP256SHA256, NSEC, "
            "unlimited KSK + 90-day auto-rolled ZSK.', "
            "true, 'ecdsap256sha256', 0, 90, false, 0, 0, false) "
            "ON CONFLICT (name) DO NOTHING"
        ).bindparams(id=_DEFAULT_POLICY_ID)
    )


def downgrade() -> None:
    op.drop_constraint("fk_dns_zone_dnssec_policy", "dns_zone", type_="foreignkey")
    op.drop_column("dns_zone", "dnssec_policy_id")
    op.drop_index("ix_dnssec_key_zone", table_name="dnssec_key")
    op.drop_table("dnssec_key")
    op.drop_index("ix_dnssec_policy_name", table_name="dnssec_policy")
    op.drop_table("dnssec_policy")
