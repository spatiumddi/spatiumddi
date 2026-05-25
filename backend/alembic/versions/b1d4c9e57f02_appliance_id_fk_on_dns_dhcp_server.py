"""appliance_id FK on dns_server + dhcp_server (#197 — cascade-on-delete)

When an operator deletes an Application appliance from the Fleet UI,
the ``Appliance`` row is removed but the ``dns_server`` / ``dhcp_server``
rows the supervisor registered as part of role assignment stayed
behind — surfacing as ghost offline servers, eating long-poll
connections, generating 404-on-heartbeat noise every minute, and
counting against group quorum for HA Kea pairs.

This migration adds a nullable ``appliance_id`` FK on both tables
with ``ON DELETE CASCADE``. Populated at supervisor-register time
(see ``apply_role_assignment`` in ``backend/app/api/v1/appliance/
supervisor.py``); legacy rows registered pre-fix stay NULL and get
swept up by the delete endpoint's hostname-match fallback.

Rationale for ON DELETE CASCADE over manual sweep in the delete
endpoint: Postgres applies CASCADE atomically with the parent
delete inside the same transaction, so a partial failure (e.g. an
audit-log write failure mid-cascade) doesn't strand orphaned rows.
The endpoint also surfaces the dependents in the delete-confirm
modal so operators see the full blast radius before clicking.

Revision ID: b1d4c9e57f02
Revises: a8e3f127c094
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b1d4c9e57f02"
down_revision = "a8e3f127c094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # dns_server.appliance_id — nullable so non-appliance DNS servers
    # (operator-defined remote BIND9 / PowerDNS pointing at an
    # off-fleet box, or pre-this-migration rows) continue to work.
    op.add_column(
        "dns_server",
        sa.Column(
            "appliance_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appliance.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_dns_server_appliance_id",
        "dns_server",
        ["appliance_id"],
    )

    # dhcp_server.appliance_id — same shape, same rationale. The
    # existing ``ix_dhcp_server_group_id`` doesn't help here because
    # the lookup pattern is "every dhcp_server FOR this appliance"
    # not "every dhcp_server IN this group".
    op.add_column(
        "dhcp_server",
        sa.Column(
            "appliance_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appliance.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_dhcp_server_appliance_id",
        "dhcp_server",
        ["appliance_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dhcp_server_appliance_id", table_name="dhcp_server")
    op.drop_column("dhcp_server", "appliance_id")
    op.drop_index("ix_dns_server_appliance_id", table_name="dns_server")
    op.drop_column("dns_server", "appliance_id")
