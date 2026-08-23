"""DNS query-log rcode + BIND response logging (issue #914)

Adds the answer side of the query log:

* ``dns_query_log_entry.rcode`` / ``.answer_count`` — what the client was
  actually told. Nullable, and NULL means UNKNOWN rather than NOERROR:
  an unrecorded outcome rendered as "fine" is precisely the wrong answer
  for the operator who is trying to work out whether DNS is the fault.
* ``dns_server_options.response_log_enabled`` — the opt-in that makes
  BIND emit the ``responses`` category those two columns are filled
  from. Defaults to false, so an existing install renders a
  byte-identical ``named.conf`` until an operator turns it on.

Revision ID: f1c7a92e4b06
Revises: e9c2d47b1a63
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f1c7a92e4b06"
down_revision = "e9c2d47b1a63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dns_query_log_entry", sa.Column("rcode", sa.String(length=16), nullable=True))
    op.add_column("dns_query_log_entry", sa.Column("answer_count", sa.Integer(), nullable=True))
    op.add_column(
        "dns_server_options",
        sa.Column(
            "response_log_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_options", "response_log_enabled")
    op.drop_column("dns_query_log_entry", "answer_count")
    op.drop_column("dns_query_log_entry", "rcode")
