"""wol_run.verify_params — per-run verify config for ad-hoc wakes (#596 Phase 1b)

Ad-hoc single-host wakes (``POST /ipam/addresses/{id}/wake``) can now opt into
the same post-wake verify + bounded re-wake chain that scheduled runs get. They
mint an ephemeral ``WolRun`` with ``schedule_id = NULL``, so there is no parent
``wol_schedule`` row for :func:`app.tasks.wol_scheduler._verify_run` to read the
verify config (method / wait / retries) and the re-wake send knobs (vantage /
port / repeat) from — it would silently fall back to hardcoded defaults and
ignore whatever the operator asked for.

``verify_params`` is that per-run snapshot. Nullable: scheduled runs leave it
NULL and keep reading their live ``wol_schedule`` row (so an operator edit
mid-flight still takes effect, unchanged from #586). Only ad-hoc runs populate
it, and for them it is the sole source of truth.

Shape (all keys optional; the reader falls back per-key):

    {"method": "auto", "wait_seconds": 60, "retries": 1,
     "vantage": {"kind": "server", "id": null},
     "port": 9, "repeat_count": 1, "repeat_interval_ms": 0}

Pure-additive nullable column add — safe under the expand/contract rolling
upgrade contract (an N-1 pod never reads it). No new table, no new feature
module (reuses ``tools.wake_scheduler``).

Revision ID: c9a2f61e740b
Revises: d1f4b7c92e58
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9a2f61e740b"
down_revision: str | None = "d1f4b7c92e58"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Nullable ⇒ no server_default needed; NULL means "read the schedule row",
    # which is exactly the pre-#596 behaviour for every existing run.
    op.add_column(
        "wol_run",
        sa.Column(
            "verify_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("wol_run", "verify_params")
