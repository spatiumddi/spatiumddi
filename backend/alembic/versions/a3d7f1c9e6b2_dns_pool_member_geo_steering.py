"""dns pool member geo / topology-aware steering columns (#530)

Adds a *serving scope* to ``dns_pool_member`` so GSLB pools can steer
clients to the nearest datacenter instead of handing every client the
same healthy rrset:

* ``serving_cidrs`` — JSONB list of client CIDR strings the member
  should be served to. Empty ⇒ default target (served to everyone).
* ``site_id`` — optional FK to ``site``; the site's linked subnets
  contribute client CIDRs to the member's scope. ``ON DELETE SET NULL``
  so deleting a Site just drops the association.

Both are additive; existing members default to empty scope (``[]`` /
NULL), preserving the current health-only behaviour byte-for-byte.

Rendering keys purely on **resolver source IP** for v1 (EDNS Client
Subnet is a documented future improvement). See
``app.services.dns.pool_geo``.

Revision ID: a3d7f1c9e6b2
Revises: c9d4a1e8b672
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3d7f1c9e6b2"
down_revision: str | None = "c9d4a1e8b672"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "dns_pool_member",
        sa.Column(
            "serving_cidrs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dns_pool_member",
        sa.Column("site_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_dns_pool_member_site",
        "dns_pool_member",
        ["site_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_dns_pool_member_site",
        "dns_pool_member",
        "site",
        ["site_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_dns_pool_member_site", "dns_pool_member", type_="foreignkey")
    op.drop_index("ix_dns_pool_member_site", table_name="dns_pool_member")
    op.drop_column("dns_pool_member", "site_id")
    op.drop_column("dns_pool_member", "serving_cidrs")
