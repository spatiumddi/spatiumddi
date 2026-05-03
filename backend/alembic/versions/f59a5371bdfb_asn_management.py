"""asn management — data model + rpki roa table

Revision ID: f59a5371bdfb
Revises: 0f83a227b16d
Create Date: 2026-05-02 00:00:00.000000

Phase 1 of the ASN management roadmap (issue #85). Two new tables:

* ``asn`` — first-class entity for the autonomous systems carrying our
  IP space. ``number`` is BigInteger to fit the full 32-bit AS range
  (Postgres ``integer`` tops out at ~2.1B, which would silently
  truncate any 32-bit private AS at 4_200_000_000+). ``kind`` and
  ``registry`` are denormalised so list queries can filter without
  re-deriving from ``number`` on every read; both get recomputed by
  the API on every write.
* ``asn_rpki_roa`` — RPKI Route Origin Authorization records the AS
  is authorised to originate. Schema only; the RIPE / Cloudflare /
  Routinator pull job lives in a follow-up issue. ``valid_to`` is
  indexed because the alert evaluator scans it on every tick.

The four BGP-relationship FKs (``ip_space.asn_id``, ``ip_block.asn_id``,
``router.local_asn_id``, ``vrf.asn_id``) ride along when those tables
get touched in their own waves — nothing on this side blocks them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f59a5371bdfb"
down_revision: Union[str, None] = "0f83a227b16d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "asn",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column(
            "name",
            sa.String(length=255),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column(
            "description",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(length=16),
            server_default=sa.text("'public'"),
            nullable=False,
        ),
        sa.Column("holder_org", sa.String(length=512), nullable=True),
        sa.Column(
            "registry",
            sa.String(length=16),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("whois_last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "whois_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "whois_state",
            sa.String(length=16),
            server_default=sa.text("'n/a'"),
            nullable=False,
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number", name="uq_asn_number"),
    )
    op.create_index("ix_asn_kind", "asn", ["kind"])
    op.create_index("ix_asn_registry", "asn", ["registry"])
    op.create_index("ix_asn_whois_state", "asn", ["whois_state"])
    op.create_index("ix_asn_holder_org", "asn", ["holder_org"])

    op.create_table(
        "asn_rpki_roa",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("asn_id", sa.UUID(), nullable=False),
        sa.Column("prefix", postgresql.CIDR(), nullable=False),
        sa.Column("max_length", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trust_anchor", sa.String(length=16), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'valid'"),
            nullable=False,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["asn_id"], ["asn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "asn_id",
            "prefix",
            "max_length",
            "trust_anchor",
            name="uq_asn_rpki_roa",
        ),
    )
    op.create_index("ix_asn_rpki_roa_asn", "asn_rpki_roa", ["asn_id"])
    op.create_index("ix_asn_rpki_roa_state", "asn_rpki_roa", ["state"])
    op.create_index("ix_asn_rpki_roa_valid_to", "asn_rpki_roa", ["valid_to"])


def downgrade() -> None:
    op.drop_index("ix_asn_rpki_roa_valid_to", table_name="asn_rpki_roa")
    op.drop_index("ix_asn_rpki_roa_state", table_name="asn_rpki_roa")
    op.drop_index("ix_asn_rpki_roa_asn", table_name="asn_rpki_roa")
    op.drop_table("asn_rpki_roa")

    op.drop_index("ix_asn_holder_org", table_name="asn")
    op.drop_index("ix_asn_whois_state", table_name="asn")
    op.drop_index("ix_asn_registry", table_name="asn")
    op.drop_index("ix_asn_kind", table_name="asn")
    op.drop_table("asn")
