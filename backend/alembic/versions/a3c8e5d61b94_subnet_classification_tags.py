"""Subnet classification tags (issue #75).

Revision ID: a3c8e5d61b94
Revises: f4a6c8b2e571
Create Date: 2026-05-03 18:00:00.000000

Adds three first-class boolean flags on ``subnet`` for compliance
scope tagging — distinct from the freeform ``tags`` JSONB blob so a
"show me every PCI subnet" query is a clean indexed predicate, not
a JSONB containment scan. Default false on all existing rows.

Each column is independently indexed for the Compliance dashboard
filter — operators ask for one boolean at a time ("PCI subnets",
"HIPAA subnets", "internet-facing subnets"). A composite index
across all three would only help "subnets that are PCI AND HIPAA",
which isn't the documented query shape.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c8e5d61b94"
down_revision: Union[str, None] = "f4a6c8b2e571"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "pci_scope",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "hipaa_scope",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "internet_facing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_index(
        "ix_subnet_pci_scope",
        "subnet",
        ["pci_scope"],
        postgresql_where=sa.text("pci_scope = true"),
    )
    op.create_index(
        "ix_subnet_hipaa_scope",
        "subnet",
        ["hipaa_scope"],
        postgresql_where=sa.text("hipaa_scope = true"),
    )
    op.create_index(
        "ix_subnet_internet_facing",
        "subnet",
        ["internet_facing"],
        postgresql_where=sa.text("internet_facing = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_subnet_internet_facing", table_name="subnet")
    op.drop_index("ix_subnet_hipaa_scope", table_name="subnet")
    op.drop_index("ix_subnet_pci_scope", table_name="subnet")
    op.drop_column("subnet", "internet_facing")
    op.drop_column("subnet", "hipaa_scope")
    op.drop_column("subnet", "pci_scope")
