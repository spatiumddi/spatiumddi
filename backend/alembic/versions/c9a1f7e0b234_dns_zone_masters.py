"""dns_zone masters (secondary/stub primaries)

Revision ID: c9a1f7e0b234
Revises: d1e7c4a90fb3
Create Date: 2026-05-30 00:00:00.000000

Adds ``dns_zone.masters`` (issue #336) — the JSONB list of primary
(master) server IPs a secondary / stub zone transfers FROM. Without it,
``type slave;`` / ``type stub;`` zones render with no ``primaries { ... };``
clause and ``named-checkconf`` rejects the config. Carried as an empty
list default for existing rows; only meaningful for secondary / stub
zone types (ignored for primary / forward).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9a1f7e0b234"
# Linearised after the #337 relay-addresses migration so the branch has a
# single Alembic head (both were authored off d1e7c4a90fb3 in parallel).
down_revision: str | None = "a1c4f7e92b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dns_zone",
        sa.Column(
            "masters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_zone", "masters")
