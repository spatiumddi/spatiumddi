"""appliance LLDP settings (issue #343)

Adds the ``lldp_*`` columns to ``platform_settings`` for host-managed lldpd
on the appliance — same host-config plane as SNMP (#153) / chrony (#154).
All columns carry a server_default so the singleton row backfills cleanly;
only meaningful on appliance hosts (ignored on docker / k8s control planes).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8c3f2a9e147"
down_revision: str | None = "a7f3c1e85d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_IFACE = "eth*,en*,!docker*,!veth*,!br-*,!cni0,!flannel.1"


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column("lldp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "platform_settings",
        sa.Column("lldp_tx_interval", sa.Integer(), nullable=False, server_default=sa.text("30")),
    )
    op.add_column(
        "platform_settings",
        sa.Column("lldp_tx_hold", sa.Integer(), nullable=False, server_default=sa.text("4")),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lldp_protocols",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lldp_interface_pattern",
            sa.String(length=512),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_IFACE}'"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lldp_management_pattern",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lldp_sys_name", sa.String(length=255), nullable=False, server_default=sa.text("''")
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lldp_sys_description",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lldp_med_location",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lldp_snmp_agentx", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    for col in (
        "lldp_snmp_agentx",
        "lldp_med_location",
        "lldp_sys_description",
        "lldp_sys_name",
        "lldp_management_pattern",
        "lldp_interface_pattern",
        "lldp_protocols",
        "lldp_tx_hold",
        "lldp_tx_interval",
        "lldp_enabled",
    ):
        op.drop_column("platform_settings", col)
