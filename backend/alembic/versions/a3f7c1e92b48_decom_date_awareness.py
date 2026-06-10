"""Decom-date awareness — first-class decom_date on Subnet + IPAddress (#46).

Revision ID: a3f7c1e92b48
Revises: d1b8f4a92c30
Create Date: 2026-06-09 00:00:00

Adds a nullable ``DATE`` ``decom_date`` column (planned decommission day)
to ``subnet`` + ``ip_address``, each with a single-column index so the
``decom_expiring`` alert rule and the admin dashboard widget can scan
"decom within N days" cheaply. NULL = no scheduled decom. Mirrors the
``Date``-column precedent in ``network_service`` (term_start/end_date).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3f7c1e92b48"
down_revision: str | None = "d1b8f4a92c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subnet", sa.Column("decom_date", sa.Date(), nullable=True))
    op.create_index("ix_subnet_decom_date", "subnet", ["decom_date"])

    op.add_column("ip_address", sa.Column("decom_date", sa.Date(), nullable=True))
    op.create_index("ix_ip_address_decom_date", "ip_address", ["decom_date"])


def downgrade() -> None:
    op.drop_index("ix_ip_address_decom_date", table_name="ip_address")
    op.drop_column("ip_address", "decom_date")

    op.drop_index("ix_subnet_decom_date", table_name="subnet")
    op.drop_column("subnet", "decom_date")
