"""dhcp_scope.provider_refs — cloud-provider object ownership (#630)

Records the provider-side DHCP object a scope owns, per cloud DHCP server, so an
agentless push (FortiGate) never adopts / overwrites / deletes an object the
operator hand-managed on the device. Shape::

    {"<dhcp_server_uuid>": {"mkey": <int>, "interface": "<name>"}}

Nullable + no backfill: existing rows own nothing until their next push records
a marker (the FortiGate driver is unreleased, so there are no live scopes whose
ownership needs reconstructing).

Revision ID: a1f4c7e92b30
Revises: d7b3f2a9c15e
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1f4c7e92b30"
down_revision = "d7b3f2a9c15e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dhcp_scope",
        sa.Column("provider_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dhcp_scope", "provider_refs")
