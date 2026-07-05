"""bgp lg route: route_distinguisher for VPNv4/VPNv6 (issue #566 Phase 6)

Revision ID: f7883fa6d413
Revises: 531494dbf44c
Create Date: 2026-07-05 16:30:00.000000

Adds ``bgp_lg_route.route_distinguisher`` (NOT NULL, default ``''``) so a
VPNv4/VPNv6 path's Route Distinguisher participates in the row's identity.

Without this, two different VRFs' overlapping customer prefixes learned
via the same peer + next-hop (an ordinary shape — the PE's own loopback
is the next-hop for every VRF it serves) collide on the existing
``uq_bgp_lg_route (peer_id, prefix, next_hop)`` constraint and silently
overwrite each other — RD's entire purpose per RFC 4364 is disambiguating
exactly this. ``''`` (not NULL) for plain ipv4-unicast/ipv6-unicast
routes, since Postgres treats every NULL as distinct in a UNIQUE
constraint — a nullable column here would silently defeat the existing
dedup semantics for ordinary unicast routes.

Widens ``uq_bgp_lg_route`` to
``(peer_id, prefix, next_hop, route_distinguisher)`` and adds a partial
index over ``route_distinguisher != ''`` (the VRF "Learned VPN Routes"
tab / admin debug queries).

Additive only. Downgrade drops the column/constraint/index — this makes
``scripts/lint_migrations.py`` flag the downgrade ``drop_column``; run
``python3 scripts/lint_migrations.py --baseline`` and commit the updated
baseline file alongside this migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7883fa6d413"
down_revision: Union[str, None] = "531494dbf44c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bgp_lg_route",
        sa.Column(
            "route_distinguisher", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    op.drop_constraint("uq_bgp_lg_route", "bgp_lg_route", type_="unique")
    op.create_unique_constraint(
        "uq_bgp_lg_route",
        "bgp_lg_route",
        ["peer_id", "prefix", "next_hop", "route_distinguisher"],
    )
    op.create_index(
        "ix_bgp_lg_route_rd",
        "bgp_lg_route",
        ["route_distinguisher"],
        postgresql_where=sa.text("route_distinguisher != ''"),
    )


def downgrade() -> None:
    op.drop_index("ix_bgp_lg_route_rd", table_name="bgp_lg_route")
    op.drop_constraint("uq_bgp_lg_route", "bgp_lg_route", type_="unique")
    op.create_unique_constraint(
        "uq_bgp_lg_route", "bgp_lg_route", ["peer_id", "prefix", "next_hop"]
    )
    op.drop_column("bgp_lg_route", "route_distinguisher")
