"""DNS-over-QUIC listener options (issue #741).

Adds ``doq_enabled`` / ``doq_port`` to ``dns_server_options``, alongside
the DoT / DoH knobs #50 added.

Technitium-only in practice — BIND9 has no DoQ listener and pdns-auth
speaks none of the encrypted transports — but the column lives on the
shared options row like its DoT/DoH siblings, with the driver gate
enforced at the API boundary rather than in the schema. Same shape as
``dot_enabled`` / ``dot_port``: default-off, so an existing install
renders byte-identically until an operator opts in.

``doq_port`` defaults to 853, the RFC 9250 default, which is the same
number DoT uses — that is correct and not a copy-paste slip: DoQ is UDP
and DoT is TCP, so the two do not collide. The firewall layer has to open
udp/853, not tcp/853, which is why the two are separate columns rather
than one shared port.

Revision ID: b2d47f1c8a30
Revises: 6a668dd451d5
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b2d47f1c8a30"
down_revision: str | None = "6a668dd451d5"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "dns_server_options",
        sa.Column(
            "doq_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "doq_port",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("853"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_options", "doq_port")
    op.drop_column("dns_server_options", "doq_enabled")
