"""BGP peering relationships + Router.local_asn_id.

Revision ID: d3f2a51c8e76
Revises: c9f1e47d2a83
Create Date: 2026-05-03 01:00:00.000000

Closes the BGP-relationships scope of issue #85:

1. ``router.local_asn_id`` — nullable FK to ``asn.id``. Stamps which
   AS the router originates routes from. ``ON DELETE SET NULL`` so a
   deleted ASN row doesn't drop the router itself; the operator
   relinks to a replacement AS.

2. ``bgp_peering`` — operator-curated graph of BGP relationships
   between tracked ASes (peer / customer / provider / sibling).
   ``(local_asn_id, peer_asn_id, relationship)`` is unique so the
   same pair can't be entered twice with the same relationship type.
   Both FKs are ``ON DELETE CASCADE`` because a peering row is
   meaningless once one of its endpoints is gone.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "d3f2a51c8e76"
down_revision: Union[str, None] = "c9f1e47d2a83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. router.local_asn_id ────────────────────────────────────────────
    op.add_column(
        "router",
        sa.Column("local_asn_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_router_local_asn_id", "router", ["local_asn_id"])
    op.create_foreign_key(
        "fk_router_local_asn",
        "router",
        "asn",
        ["local_asn_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── 2. bgp_peering ────────────────────────────────────────────────────
    op.create_table(
        "bgp_peering",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("local_asn_id", UUID(as_uuid=True), nullable=False),
        sa.Column("peer_asn_id", UUID(as_uuid=True), nullable=False),
        sa.Column("relationship_type", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
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
            ["local_asn_id"], ["asn.id"], name="fk_bgp_peering_local", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["peer_asn_id"], ["asn.id"], name="fk_bgp_peering_peer", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "local_asn_id",
            "peer_asn_id",
            "relationship_type",
            name="uq_bgp_peering",
        ),
    )
    op.create_index("ix_bgp_peering_local", "bgp_peering", ["local_asn_id"])
    op.create_index("ix_bgp_peering_peer", "bgp_peering", ["peer_asn_id"])
    op.create_index(
        "ix_bgp_peering_relationship", "bgp_peering", ["relationship_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_bgp_peering_relationship", table_name="bgp_peering")
    op.drop_index("ix_bgp_peering_peer", table_name="bgp_peering")
    op.drop_index("ix_bgp_peering_local", table_name="bgp_peering")
    op.drop_table("bgp_peering")

    op.drop_constraint("fk_router_local_asn", "router", type_="foreignkey")
    op.drop_index("ix_router_local_asn_id", table_name="router")
    op.drop_column("router", "local_asn_id")
