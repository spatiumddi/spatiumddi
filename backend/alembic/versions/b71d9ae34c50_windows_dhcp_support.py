"""windows_dhcp support — DHCPServer credentials + lease-pull settings

Adds:
  * ``dhcp_server.credentials_encrypted`` — Fernet-encrypted JSON blob for
    driver-specific admin credentials (today: Windows DHCP WinRM user /
    password). Agent-based drivers (kea, isc_dhcp) leave it NULL.
  * ``platform_settings.dhcp_pull_leases_enabled`` / ``_interval_minutes``
    / ``_last_run_at`` — gate + cadence + bookkeeping for the scheduled
    lease-pull task (mirrors the DNS ``dns_pull_from_server_*`` triplet).

Revision ID: b71d9ae34c50
Revises: f1a8d2c5e413
Create Date: 2026-04-17 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b71d9ae34c50"
down_revision: str | None = "f1a8d2c5e413"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dhcp_server",
        sa.Column("credentials_encrypted", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dhcp_pull_leases_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dhcp_pull_leases_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dhcp_pull_leases_last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "dhcp_pull_leases_last_run_at")
    op.drop_column("platform_settings", "dhcp_pull_leases_interval_minutes")
    op.drop_column("platform_settings", "dhcp_pull_leases_enabled")
    op.drop_column("dhcp_server", "credentials_encrypted")
