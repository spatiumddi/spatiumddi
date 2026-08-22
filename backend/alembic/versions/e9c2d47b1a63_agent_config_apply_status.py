"""Agent config-apply status on DNS / DHCP / looking-glass server rows.

Issue #882 — agents cache a last-known-good bundle and now revert to it
when a new one fails to apply. A revert has to be visible: without these
columns the control plane cannot tell a server that converged from one
that is healthy, reachable, answering queries, and running a config the
operator never approved.

The agents already reported this — the DNS and DHCP heartbeat request
models have declared a ``config: dict`` field since they were written, and
both handlers ignored it. This migration is the storage that makes the
field mean something.

Revision ID: e9c2d47b1a63
Revises: f4b91d38a70c
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e9c2d47b1a63"
down_revision = "f4b91d38a70c"
branch_labels = None
depends_on = None


# Same four columns on each agent-managed server table. NULLable throughout:
# NULL means "this agent has never reported", which covers both a pre-#882
# agent in the field and an agentless driver (Windows DNS, the cloud DNS
# providers, ``technitium_api``) that has no apply loop to report from. The
# read side must treat NULL as UNKNOWN and never as ``ok`` — an agent too old
# to send a verdict is exactly the case where a silent revert could hide.
_TABLES = ("dns_server", "dhcp_server", "looking_glass_collector")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("config_apply_status", sa.String(20), nullable=True))
        op.add_column(table, sa.Column("config_apply_error", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("config_failed_etag", sa.String(128), nullable=True))
        op.add_column(
            table,
            sa.Column("config_apply_at", sa.DateTime(timezone=True), nullable=True),
        )
        # Partial index over the failing states only. The alert sweep and the
        # server-list chip both ask "which servers have NOT converged", and on
        # a healthy fleet that matches ~nothing — indexing the whole column
        # would be almost entirely 'ok' rows nobody queries for.
        op.create_index(
            f"ix_{table}_config_apply_failed",
            table,
            ["config_apply_status"],
            unique=False,
            postgresql_where=sa.text(
                "config_apply_status IS NOT NULL AND config_apply_status <> 'ok'"
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_config_apply_failed", table_name=table)
        op.drop_column(table, "config_apply_at")
        op.drop_column(table, "config_failed_etag")
        op.drop_column(table, "config_apply_error")
        op.drop_column(table, "config_apply_status")
