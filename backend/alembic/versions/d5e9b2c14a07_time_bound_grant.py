"""time_bound_grant — temporary, auto-expiring RBAC grants (#65).

A row grants one ``{action, resource_type, resource_id?}`` permission to a
group until ``expires_at``. ``user_has_permission`` consults live grants as
an additive union over the static role grants, and a 60 s beat sweep
soft-revokes (``revoked_at``) rows whose ``expires_at`` has passed. Rows are
never hard-deleted — the revoked row stays as the audit / history breadcrumb.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "d5e9b2c14a07"
down_revision: str | None = "a3f7c1e92b48"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "time_bound_grant",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("group.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column(
            "granted_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Composite index for the per-request "live grants for these groups"
    # query: filter group_id IN (...) AND revoked_at IS NULL AND
    # expires_at > now().
    op.create_index(
        "ix_time_bound_grant_live",
        "time_bound_grant",
        ["group_id", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_time_bound_grant_live", table_name="time_bound_grant")
    op.drop_table("time_bound_grant")
