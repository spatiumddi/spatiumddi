"""ACME client — manual DNS-01 fallback + allow_manual flag (#438 Phase 3)

Phase 3 of the embedded ACME client: lets an operator issue a cert for a
domain whose authoritative DNS SpatiumDDI does NOT manage. The orchestrator
surfaces the required ``_acme-challenge`` TXT on the order, the operator
adds it at their own provider, and the orchestrator polls public DNS until
it appears before telling the CA to validate.

(DNS-01 against zones SpatiumDDI DOES manage — including cloud-hosted ones
served through the agentless Cloudflare/Route53/Azure/Google drivers —
already works in Phase 1 via the shared record_ops pipeline; this migration
only adds the *manual* fallback state.)

Adds two ``acme_order`` columns:

* ``allow_manual`` — operator opted into the manual fallback for this order.
* ``manual_challenges`` — JSONB list of ``{fqdn, record_name, txt_value}``
  the orchestrator publishes for the UI to display while the order waits.

Revision ID: b2f5a9c41e07
Revises: a7f2c9e4d1b8
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2f5a9c41e07"
down_revision: str | None = "a7f2c9e4d1b8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "acme_order",
        sa.Column("allow_manual", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "acme_order",
        sa.Column(
            "manual_challenges",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("acme_order", "manual_challenges")
    op.drop_column("acme_order", "allow_manual")
