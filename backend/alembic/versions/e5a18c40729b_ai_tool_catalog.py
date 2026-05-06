"""ai tool catalog (issue #101 follow-up)

Adds ``platform_settings.ai_tools_enabled`` — operator-set explicit
allowlist for the Operator Copilot's tool registry.

Semantics:

* ``NULL`` (default) — every tool that ships with ``default_enabled
  = True`` is exposed. New tools added in future releases follow
  their declared default.
* Non-NULL list — exactly these tools are exposed regardless of
  declared default. Empty list disables every tool (chat still
  works, just with no tools — useful for sanitised demo modes).

Per-provider ``AIProvider.enabled_tools`` then narrows further; both
layers compose so an operator can globally enable a tool but
restrict it on a small-context provider.

Revision ID: e5a18c40729b
Revises: d92f4a18c763
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "e5a18c40729b"
down_revision = "d92f4a18c763"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "ai_tools_enabled",
            JSONB,
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "ai_tools_enabled")
