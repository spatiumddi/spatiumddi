"""UniFi → Router cascade FK so the reconciler can mirror VLANs cleanly.

Revision ID: c3e8b57a2f14
Revises: b2c84f7a91d3
Create Date: 2026-05-07 00:30:00

The UniFi reconciler now creates one ``router`` row per controller
and one ``vlan`` row per UniFi network with a 802.1Q tag, then
points each mirrored Subnet's ``vlan_ref_id`` at the matching VLAN.

Adding ``router.unifi_controller_id`` (cascade on delete) means
removing a controller drops its Router (and the Router's VLANs
via the existing ``cascade='all, delete-orphan'`` on the SQLAlchemy
relationship), keeping the VLAN tree consistent.

The column is nullable because operator-managed Router rows
predate UniFi integration and stay untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8b57a2f14"
down_revision: str | None = "b2c84f7a91d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "router",
        sa.Column(
            "unifi_controller_id",
            sa.UUID(),
            sa.ForeignKey("unifi_controller.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_router_unifi_controller_id", "router", ["unifi_controller_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_router_unifi_controller_id", table_name="router")
    op.drop_column("router", "unifi_controller_id")
