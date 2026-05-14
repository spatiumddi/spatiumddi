"""Issue #170 Wave C3 — appliance.firewall_extra (operator override).

Adds the per-appliance free-form nftables fragment column. Lets an
operator paste raw nft rules that get rendered *after* the role-
driven block so they can allow eg ``udp dport 161 accept`` from a
specific subnet without the supervisor stomping on it. The
role-driven mgmt + per-role rules + extra fragment together form
the ``spatium-role.nft`` drop-in the supervisor renders every
heartbeat (Wave C3 supervisor side).

Stored as ``TEXT`` (not JSONB) — operators paste nft syntax
verbatim; the supervisor validates by running ``nft -c -f <file>``
in dry-run mode before atomically replacing the live drop-in. A
syntax error rejects the assignment but never leaves the appliance
firewalled-open or firewalled-shut.

NULLABLE — operators who never set it never need to think about it;
the role-driven block renders by itself. ``""`` means "operator
intentionally cleared it"; ``None`` means "operator never touched
it." Functionally equivalent today but the distinction may matter
later for audit + UI affordances.

Revision ID: 78bfb374d56d
Revises: ee86cea94cf5
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "78bfb374d56d"
down_revision: str | None = "ee86cea94cf5"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("firewall_extra", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "firewall_extra")
