"""wol_run_target.verify_evidence — per-target liveness evidence trail (#596 Phase 3)

``wol_run_target.verify_method`` records which source *settled* a host's verdict,
but not what the other sources said. When a host reads as down, the operator's
next question is always "down according to what?" — and the answer differs
sharply in what it implies: "ping timed out, TCP refused nothing, last seen 3 days
ago" is a dead box, while "ping timed out, TCP connected" would be a contradiction
worth investigating.

``verify_evidence`` is an ordered JSONB array, one entry per source actually
consulted on the final pass:

    [{"source": "ping", "up": false, "detail": "no reply", "observed_at": "..."},
     {"source": "tcp",  "up": false, "detail": "no open/refused port", ...},
     {"source": "seen", "up": false, "detail": "no sighting since the wake", ...}]

Nullable ⇒ rows written before this migration (and rows no source could run
against) simply carry NULL, which the UI renders as "no evidence recorded".

Pure-additive nullable column add — safe under the expand/contract rolling
upgrade contract (an N-1 pod never reads it).

Revision ID: e4b8073af215
Revises: a71e5c30d9f4
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e4b8073af215"
down_revision: str | None = "a71e5c30d9f4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "wol_run_target",
        sa.Column(
            "verify_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("wol_run_target", "verify_evidence")
