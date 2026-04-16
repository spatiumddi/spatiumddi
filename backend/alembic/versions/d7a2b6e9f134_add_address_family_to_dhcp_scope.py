"""add address_family to dhcp_scope

Introduces ``dhcp_scope.address_family`` so the Kea driver can render either
``Dhcp4`` or ``Dhcp6`` config blocks from the same scope rows. Backfills
existing rows to ``ipv4`` (every scope created before IPv6 support was a
v4 scope).

NOTE (wave D): this migration was authored targeting ``b4d1c9e2f3a7`` as
parent because that is the actual head in this worktree. The wave task
suggested ``c4d9a1e6f827`` — that revision does not exist here. If other
agents add migrations on top of ``b4d1c9e2f3a7`` in parallel branches the
user should rebase this file's ``down_revision`` at merge time.

Revision ID: d7a2b6e9f134
Revises: b4d1c9e2f3a7
Create Date: 2026-04-16 17:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d7a2b6e9f134"
down_revision: Union[str, None] = "b4d1c9e2f3a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "address_family",
            sa.String(length=4),
            nullable=False,
            server_default="ipv4",
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_scope", "address_family")
