"""backup_target.write_only — write-only / immutable destinations (#989)

The credential that writes an archive could also delete it, and the
retention sweep ran with that credential on every scheduled run. So a
compromised control plane, a leaked ``backup_target.config``, or an
attacker who reached the API could wipe the backups with the same key
that made them. That is the one gap in the destination story worth
calling a hole rather than a convenience.

Defaults FALSE, so nothing changes for an existing target. Turning it on
skips the prune, refuses the archive-delete route, tolerates a refused
delete in the connection probe, and makes an undrillable destination
report UNVERIFIED rather than healthy.

The S3 Object Lock settings that pair with it live in the driver's
``config`` JSONB and need no column.

Revision ID: b8f4c02e7a19
Revises: e5b1d47a9c62
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa

revision = "b8f4c02e7a19"
down_revision = "e5b1d47a9c62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backup_target",
        sa.Column(
            "write_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("backup_target", "write_only")
