"""tld_registry_snapshot — operator-refreshed IANA root-zone TLD list (#986)

Singleton table (id=1) holding a copy of IANA's ``tlds-alpha-by-domain.txt``
that supersedes the bundled ``app/data/iana_tlds.json`` when its version is
newer. Postgres rather than disk so the list is the same on every node of a
multi-node control plane.

Revision ID: b6f2c04a71d8
Revises: f3b8d21c74ae
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b6f2c04a71d8"
down_revision: str | None = "f3b8d21c74ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tld_registry_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "tlds",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # TimestampMixin. Explicit server defaults: create_all fills these
        # from the Python side in tests, but a fresh install runs the
        # migration and would NOT-NULL-violate without them.
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
        sa.PrimaryKeyConstraint("id"),
        # Belt-and-braces on the singleton contract. The service layer
        # always writes id=1, but a stray insert would make
        # ``load_snapshot``'s LIMIT 1 non-deterministic — one api pod
        # preferring one row and its neighbour another.
        sa.CheckConstraint("id = 1", name="ck_tld_registry_snapshot_singleton"),
    )


def downgrade() -> None:
    op.drop_table("tld_registry_snapshot")
