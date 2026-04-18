"""Add ``dns_server.credentials_encrypted`` for Windows DNS Path B (WinRM).

Mirrors the DHCP-side ``credentials_encrypted`` column added for Windows DHCP
over WinRM. Stores a Fernet-encrypted JSON dict of WinRM auth settings
(``username``, ``password``, ``winrm_port``, ``transport``, ``use_tls``,
``verify_tls``). NULL for agent-managed drivers (bind9) and for the existing
Windows DNS Path A rows that only do RFC 2136 record CRUD — those never
needed WinRM.

Revision ID: d3f1ab7c8e02
Revises: e5b831f02db9
Create Date: 2026-04-18 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3f1ab7c8e02"
down_revision: str | None = "e5b831f02db9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dns_server",
        sa.Column("credentials_encrypted", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dns_server", "credentials_encrypted")
