"""domain phase 2 — scheduled refresh + alert rule columns

Revision ID: 4a9e7c2d18b3
Revises: 4a7c8e3d51b9
Create Date: 2026-05-02 00:01:00.000000

Phase 2 of issue #87. Adds:

* ``platform_settings.domain_whois_interval_hours`` — operator-tunable
  cadence for the new ``app.tasks.domain_whois_refresh`` beat task
  (default 24 h, validator clamps to 1–168 h).
* ``alert_rule.threshold_days`` — params column for the new
  ``domain_expiring`` rule type. Mirrors the existing
  ``threshold_percent`` column shape rather than introducing a generic
  JSONB ``params`` blob — keeps the surface flat and the query path
  simple.
* ``alert_event.last_observed_value`` — JSONB snapshot of the value
  observed at the time the event fired. Used by the two
  "fires once on transition" domain rule types
  (``domain_registrar_changed`` / ``domain_dnssec_status_changed``)
  to dedupe re-firing on the same value-pair.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4a9e7c2d18b3"
down_revision: Union[str, None] = "4a7c8e3d51b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "domain_whois_interval_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
    )
    op.add_column(
        "alert_rule",
        sa.Column("threshold_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "alert_event",
        sa.Column(
            "last_observed_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_event", "last_observed_value")
    op.drop_column("alert_rule", "threshold_days")
    op.drop_column("platform_settings", "domain_whois_interval_hours")
