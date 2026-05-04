"""DNS split-horizon publishing at the IPAM layer (issue #25).

Revision ID: e7c4f29ad581
Revises: d5b8a3f12e64
Create Date: 2026-05-04 04:00:00.000000

Lets one IPAM row publish A/AAAA records into multiple DNS zones —
typically zones in different DNS server groups (internal vs external)
so the same hostname resolves to internal-only resolvers AND
public-facing resolvers without manual record duplication.

Additive shape:
* ``ip_address.extra_zone_ids`` — JSONB list of zone UUIDs to publish
  in addition to the existing singular ``forward_zone_id``. Default
  ``[]`` so existing rows publish exactly one record (current
  behaviour). Ordering preserved on round-trip.
* ``subnet.dns_split_horizon`` (bool, default false) — per-subnet
  opt-in for the multi-zone picker. When off the picker stays
  single-group like today; when on it becomes a multi-select grouped
  by DNS server group.
* ``ip_block.dns_split_horizon`` (bool, default false) — same shape,
  inheritable from a block to its descendant subnets via the existing
  ``dns_inherit_settings`` walk.
* ``dns_server_group.is_public_facing`` (bool, default false) — flag
  so the safety guard knows which groups are exposed to the public
  internet. When an operator pins a private subnet's IP into a
  public-facing group, the create / update path returns 422 with a
  ``requires_confirmation`` warning that the modal turns into a
  typed-CIDR confirmation gate.

No reverse-zone changes — PTR stays singular per the issue body.
Split-horizon reverse zones are a deferred follow-up.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e7c4f29ad581"
down_revision: Union[str, None] = "d5b8a3f12e64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ip_address",
        sa.Column(
            "extra_zone_ids",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "dns_split_horizon",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "ip_block",
        sa.Column(
            "dns_split_horizon",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "dns_server_group",
        sa.Column(
            "is_public_facing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_group", "is_public_facing")
    op.drop_column("ip_block", "dns_split_horizon")
    op.drop_column("subnet", "dns_split_horizon")
    op.drop_column("ip_address", "extra_zone_ids")
