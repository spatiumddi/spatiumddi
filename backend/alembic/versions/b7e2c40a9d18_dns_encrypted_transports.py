"""dns encrypted transports: DoT / DoH listeners + encrypted upstream forwarding

Revision ID: b7e2c40a9d18
Revises: a3d9f1e64c72
Create Date: 2026-07-24 10:00:00.000000

Issue #50 — SpatiumDDI's managed DNS was Do53-only in both directions.
This adds the ``dns_server_options`` columns for both halves:

* **Inbound** — ``dot_enabled`` / ``dot_port`` and ``doh_enabled`` /
  ``doh_port`` / ``doh_path`` drive additional BIND9 ``listen-on`` clauses
  (and ``addTLSLocal`` / ``addDOHLocal`` on the dnsdist front for
  PowerDNS). The plain :53 listener is unaffected — these are additive.
* **Cert** — ``tls_certificate_id`` points at the existing
  ``appliance_certificate`` store (shared with the Web UI cert + the
  embedded ACME client from #438). ``ON DELETE SET NULL`` so deleting a
  certificate can never delete a group's whole options row; the renderer
  treats "listener on, cert NULL" as listener-off rather than emitting an
  unloadable ``tls`` block.
* **Outbound** — ``forward_transport`` (do53 | tls) plus
  ``forward_tls_hostname`` / ``forward_tls_verify`` for strict upstream
  certificate validation. BIND has no client-side HTTP transport, so
  there is deliberately no "https" value here.

Every column carries a server default matching the model default, so an
existing install renders a byte-identical named.conf until an operator
opts in.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7e2c40a9d18"
down_revision = "a3d9f1e64c72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_server_options",
        sa.Column("dot_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("dot_port", sa.Integer(), nullable=False, server_default=sa.text("853")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("doh_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("doh_port", sa.Integer(), nullable=False, server_default=sa.text("443")),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "doh_path",
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("'/dns-query'"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("tls_certificate_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dns_server_options_tls_certificate_id",
        "dns_server_options",
        "appliance_certificate",
        ["tls_certificate_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_dns_server_options_tls_certificate_id",
        "dns_server_options",
        ["tls_certificate_id"],
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "forward_transport",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'do53'"),
        ),
    )
    op.add_column(
        "dns_server_options",
        sa.Column("forward_tls_hostname", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "dns_server_options",
        sa.Column(
            "forward_tls_verify", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_server_options", "forward_tls_verify")
    op.drop_column("dns_server_options", "forward_tls_hostname")
    op.drop_column("dns_server_options", "forward_transport")
    op.drop_index("ix_dns_server_options_tls_certificate_id", table_name="dns_server_options")
    op.drop_constraint(
        "fk_dns_server_options_tls_certificate_id", "dns_server_options", type_="foreignkey"
    )
    op.drop_column("dns_server_options", "tls_certificate_id")
    op.drop_column("dns_server_options", "doh_path")
    op.drop_column("dns_server_options", "doh_port")
    op.drop_column("dns_server_options", "doh_enabled")
    op.drop_column("dns_server_options", "dot_port")
    op.drop_column("dns_server_options", "dot_enabled")
