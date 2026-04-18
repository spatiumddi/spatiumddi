"""dns_server.is_enabled — user-controlled pause flag

Distinct from ``status`` (which is health-derived and automatic).
``is_enabled=False`` makes the health-check sweep, the bi-directional
sync task, and the record-op dispatcher all skip the server — useful
when a DC is going through maintenance and you don't want SpatiumDDI
poking it.

Revision ID: c4e8f1a25d93
Revises: b71d9ae34c50
Create Date: 2026-04-17 22:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8f1a25d93"
down_revision: str | None = "b71d9ae34c50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dns_server",
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server", "is_enabled")
