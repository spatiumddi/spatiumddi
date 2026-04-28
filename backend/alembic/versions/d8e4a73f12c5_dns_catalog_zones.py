"""dns_catalog_zones

Revision ID: d8e4a73f12c5
Revises: 7c299e8a5490
Create Date: 2026-04-28 21:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e4a73f12c5"
down_revision: Union[str, None] = "7c299e8a5490"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dns_server_group",
        sa.Column(
            "catalog_zones_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "dns_server_group",
        sa.Column(
            "catalog_zone_name",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'catalog.spatium.invalid.'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_group", "catalog_zone_name")
    op.drop_column("dns_server_group", "catalog_zones_enabled")
