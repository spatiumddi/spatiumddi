"""appliance: host-migration reconcile health (#395)

Adds ``appliance.host_migration_health`` — the supervisor reads the
``host-patches-applied.json`` ledger written by ``spatium-host-migrate``
and reports, per failing patch (``ok: false``), a ``{state, attempts,
at, error?}`` entry keyed by patch id (e.g. ``001-grub-render``). Only
patches whose desired state has NOT been applied appear, so an
all-applied appliance reports ``{}``. Surfaced in the Fleet drilldown
so a failed grub.cfg re-render (or any future numbered host-patch) is
visible to the operator instead of silently preventing slot commit.

Mirrors the ``host_config_health`` column added by the preceding
migration (f4a1c9e7b2d8).

Revision ID: b3e7d1f9a204
Revises: f4a1c9e7b2d8
Create Date: 2026-06-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3e7d1f9a204"
down_revision: str | None = "a7c3e9f1b405"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "host_migration_health",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "host_migration_health")
