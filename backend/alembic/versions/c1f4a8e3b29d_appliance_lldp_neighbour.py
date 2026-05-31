"""appliance_lldp_neighbour — LLDP neighbour discovery (issue #347)

Dedicated table for LLDP neighbours an appliance host discovers via its local
lldpd (``lldpcli show neighbors``). Distinct from ``network_neighbour`` (the
SNMP-MIB-shaped switch-polled table) — this matches lldpcli's output directly.
Absence-deleted on every supervisor heartbeat ingest.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1f4a8e3b29d"
down_revision: str | None = "b8c3f2a9e147"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "appliance_lldp_neighbour",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "appliance_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("appliance.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("local_iface", sa.String(length=64), nullable=False),
        sa.Column("remote_chassis_id", sa.String(length=255), nullable=False),
        sa.Column("remote_port_id", sa.String(length=255), nullable=False),
        sa.Column("remote_port_descr", sa.String(length=255), nullable=True),
        sa.Column("remote_sys_name", sa.String(length=255), nullable=True),
        sa.Column("remote_sys_descr", sa.Text(), nullable=True),
        sa.Column("remote_mgmt_ip", sa.String(length=64), nullable=True),
        sa.Column("remote_caps", sa.String(length=255), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.UniqueConstraint(
            "appliance_id",
            "local_iface",
            "remote_chassis_id",
            "remote_port_id",
            name="uq_appliance_lldp_neighbour",
        ),
    )
    op.create_index(
        "ix_appliance_lldp_neighbour_appliance",
        "appliance_lldp_neighbour",
        ["appliance_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_appliance_lldp_neighbour_appliance", table_name="appliance_lldp_neighbour")
    op.drop_table("appliance_lldp_neighbour")
