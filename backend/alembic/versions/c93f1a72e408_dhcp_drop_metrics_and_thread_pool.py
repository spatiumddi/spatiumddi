"""#980 — per-bucket DHCP loss counters + two Kea packet-path knobs.

Two columns on ``dhcp_metric_sample`` and two on ``dhcp_server_group``.

``receive_drop`` / ``socket_drop`` are deliberately NULLABLE with no
server_default. They are not "zero until measured": zero means the agent
looked and found no loss, and NULL means nobody looked — an agent older
than this change, or one whose runtime cannot read ``/proc/net/udp``. A
default of 0 would paint the entire pre-upgrade history, and every
not-yet-upgraded agent, as a server that has never dropped a packet, which
is the exact false-reassurance this issue is about.

``kea_thread_pool_size`` defaults to 1 and IS written to existing rows,
because leaving Kea's own default in place is the bug. Kea sizes its
packet-processing pool from ``hardware_concurrency()`` — the node's CPU
count — with no regard for the cgroup share it actually holds, so on a
4 vCPU appliance it starts 4 workers that compete with the one thread
that has to drain the receive socket. Measured against kea-dhcp4 3.0.3
(memfile backend, relayed unicast) on 2026-09-06, offered 12,000 pkt/s:

    cgroup CPU   pool=1 served      pool=2         pool=4 (= "auto" here)
    0.25         18.4k-22.5k        9.6k-11.6k     6.5k-7.1k
    4.0 (none)   79.5k-95.6k        75.8k-77.5k    44.5k

Every DHCP group therefore re-renders once on upgrade and its Kea
config-reloads. ``0`` restores Kea's auto-sizing for an operator who
measures otherwise.

``kea_packet_logging`` defaults to **true**, which is exactly today's
behaviour, because turning it off removes two log codes an operator can
currently see (``DHCP4_PACKET_RECEIVED`` / ``DHCP4_PACKET_SEND``, carrying
the source address and interface). It is worth 1.30x on the same rig —
24,997-26,077 packets served against 19,026-20,403 — so it is offered as a
lever for a site at the knee rather than taken away from everyone. Same
reasoning as #637's lease cache, which also preserves observable behaviour
and lets the operator opt into the throughput.

Revision ID: c93f1a72e408
Revises: d4a9e37b2c15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c93f1a72e408"
down_revision: str | None = "d4a9e37b2c15"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "dhcp_metric_sample",
        sa.Column("receive_drop", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "dhcp_metric_sample",
        sa.Column("socket_drop", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "kea_thread_pool_size",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "kea_packet_logging",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_server_group", "kea_packet_logging")
    op.drop_column("dhcp_server_group", "kea_thread_pool_size")
    op.drop_column("dhcp_metric_sample", "socket_drop")
    op.drop_column("dhcp_metric_sample", "receive_drop")
