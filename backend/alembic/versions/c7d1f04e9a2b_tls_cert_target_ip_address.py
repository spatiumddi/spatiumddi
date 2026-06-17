"""TLS cert target → IP address linkage (#118 Phase 2)

Adds ``tls_cert_target.ip_address_id`` (FK → ip_address, ON DELETE SET NULL)
so IPAM-role-discovered targets carry their source IP and the IP detail
modal can list "certs served from this IP".

Revision ID: c7d1f04e9a2b
Revises: f3e8b1d72a9c
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7d1f04e9a2b"
down_revision: str | None = "f3e8b1d72a9c"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tls_cert_target",
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tls_cert_target_ip_address_id",
        "tls_cert_target",
        "ip_address",
        ["ip_address_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tls_cert_target_ip_address_id", "tls_cert_target", ["ip_address_id"])


def downgrade() -> None:
    op.drop_index("ix_tls_cert_target_ip_address_id", table_name="tls_cert_target")
    op.drop_constraint("fk_tls_cert_target_ip_address_id", "tls_cert_target", type_="foreignkey")
    op.drop_column("tls_cert_target", "ip_address_id")
