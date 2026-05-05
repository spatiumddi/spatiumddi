"""Widen alert_event.subject_type to fit network_service_resource (#94).

Revision ID: f2c8d49a1e76
Revises: e1d8c92a4f73
Create Date: 2026-05-05 00:00:00

The new ``service_resource_orphaned`` rule type writes events with
``subject_type='network_service_resource'`` (25 chars), which doesn't
fit the existing ``VARCHAR(20)`` column. Widen to 40 to give future
subject-type families room without another migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2c8d49a1e76"
down_revision: str | None = "e1d8c92a4f73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "alert_event",
        "subject_type",
        existing_type=sa.String(length=20),
        type_=sa.String(length=40),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "alert_event",
        "subject_type",
        existing_type=sa.String(length=40),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
