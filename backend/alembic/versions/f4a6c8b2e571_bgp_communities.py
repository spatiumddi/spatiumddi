"""BGP communities catalog (issue #88).

Revision ID: f4a6c8b2e571
Revises: e7b8c4f96a12
Create Date: 2026-05-03 03:00:00.000000

Operator-curated catalog of BGP community values + the policy
semantics they imply for each tracked ASN. Standard / well-known
rows (``RFC 1997 no-export`` etc.) live with ``asn_id IS NULL`` and
are seeded on first boot by the application — see
``app.services.bgp_communities.seed_standard``.

``value`` is stored as the on-the-wire string (``65000:100`` for
RFC 1997 regular, ``65000:100:200`` for RFC 8092 large, or the
shortcut name like ``no-export`` / ``blackhole`` for standards).
``kind`` denormalises which of the three formats applies so the UI
can group + validate without re-parsing.

Unique on ``(asn_id, value)`` — the same wire value can only mean
one thing within an AS.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "f4a6c8b2e571"
down_revision: Union[str, None] = "e7b8c4f96a12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bgp_community",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("asn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("value", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="regular"),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "inbound_action", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "outbound_action", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "tags",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.ForeignKeyConstraint(
            ["asn_id"],
            ["asn.id"],
            name="fk_bgp_community_asn",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("asn_id", "value", name="uq_bgp_community_value"),
    )
    op.create_index("ix_bgp_community_asn", "bgp_community", ["asn_id"])
    op.create_index("ix_bgp_community_kind", "bgp_community", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_bgp_community_kind", table_name="bgp_community")
    op.drop_index("ix_bgp_community_asn", table_name="bgp_community")
    op.drop_table("bgp_community")
