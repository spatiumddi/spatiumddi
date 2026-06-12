"""appliance upgrade: download integrity hints + log-tail monitoring

Issue #386. Adds three columns to ``appliance`` so a single-appliance
OS slot-upgrade (a) can be fetched when the image is served by the
appliance's own self-signed control plane, and (b) is observable in
the Fleet UI instead of failing silently:

* ``desired_slot_image_sha256`` — expected hash the host-side runner
  verifies the downloaded image against (Part A).
* ``desired_slot_image_tls_insecure`` — allow the runner to skip TLS
  cert-verify for the appliance's own self-served URL (Part A); only
  honoured host-side when an expected sha256 is present.
* ``last_upgrade_log_tail`` — tail of the host ``slot-upgrade.log``
  the supervisor ships while an apply is in-flight / failed, surfaced
  in the Fleet drilldown (Part C).

Revision ID: e1c4a9d27b63
Revises: c3f7a1d9b486
Create Date: 2026-06-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1c4a9d27b63"
down_revision: str | None = "c3f7a1d9b486"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("desired_slot_image_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "desired_slot_image_tls_insecure",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("last_upgrade_log_tail", sa.Text(), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("last_upgrade_progress", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "last_upgrade_progress")
    op.drop_column("appliance", "last_upgrade_log_tail")
    op.drop_column("appliance", "desired_slot_image_tls_insecure")
    op.drop_column("appliance", "desired_slot_image_sha256")
