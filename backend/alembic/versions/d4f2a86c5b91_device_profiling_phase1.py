"""device profiling phase 1 — auto-nmap on DHCP lease

Subnet: ``auto_profile_on_dhcp_lease`` toggle + ``auto_profile_preset``
+ ``auto_profile_refresh_days`` + ``auto_profile_inherit_settings``.
IPAddress: ``last_profiled_at`` + ``last_profile_scan_id`` FK to
``nmap_scan`` (ON DELETE SET NULL — scans can be pruned without
nuking the host pointer).

Phase 2 (passive DHCP fingerprinting via scapy + fingerbank) lands
its own migration on top of this one.

Revision ID: d4f2a86c5b91
Revises: c3e9a2b71f48
Create Date: 2026-04-30 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f2a86c5b91"
down_revision: Union[str, None] = "c3e9a2b71f48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "auto_profile_on_dhcp_lease",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "auto_profile_preset",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'service_version'"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "auto_profile_refresh_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
    )

    op.add_column(
        "ip_address",
        sa.Column("last_profiled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ip_address",
        sa.Column("last_profile_scan_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ip_address_last_profile_scan",
        "ip_address",
        "nmap_scan",
        ["last_profile_scan_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_ip_address_last_profile_scan", "ip_address", type_="foreignkey")
    op.drop_column("ip_address", "last_profile_scan_id")
    op.drop_column("ip_address", "last_profiled_at")
    op.drop_column("subnet", "auto_profile_refresh_days")
    op.drop_column("subnet", "auto_profile_preset")
    op.drop_column("subnet", "auto_profile_on_dhcp_lease")
