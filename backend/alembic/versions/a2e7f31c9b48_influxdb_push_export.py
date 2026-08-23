"""InfluxDB push-export targets (issue #889)

Creates ``influxdb_target`` — one row per InfluxDB destination the
control plane pushes line protocol to. Three declared versions (v1 / v2
/ v3) share two wire dialects; see ``app/models/influxdb.py``.

Credentials are Fernet-encrypted ``LargeBinary`` columns following the
``audit_forward_target.smtp_password_encrypted`` convention — the API
returns ``password_set`` / ``token_set`` booleans, never the value.

``last_dns_bucket_at`` / ``last_dhcp_bucket_at`` are per-source
high-water marks so a restart doesn't re-push the retention window;
they are an optimisation only, since line protocol overwrites a point
with an identical measurement + tag set + timestamp.

Revision ID: a2e7f31c9b48
Revises: f1c7a92e4b06
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a2e7f31c9b48"
down_revision = "f1c7a92e4b06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "influxdb_target",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.String(length=4), nullable=False, server_default=sa.text("'v2'")),
        sa.Column("url", sa.String(length=1024), nullable=False, server_default=sa.text("''")),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("10")),
        # v1
        sa.Column("database", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("username", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("password_encrypted", sa.LargeBinary(), nullable=True),
        # v2 / v3
        sa.Column("org", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("bucket", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column(
            "measurement_prefix",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'spatiumddi_'"),
        ),
        sa.Column(
            "push_interval_seconds", sa.Integer(), nullable=False, server_default=sa.text("60")
        ),
        sa.Column("push_dns_metrics", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("push_dhcp_metrics", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "push_subnet_utilization", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("push_dhcp_scope_leases", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_dns_bucket_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_dhcp_bucket_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_push_points", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_push_error", sa.Text(), nullable=True),
        # TimestampMixin columns need an explicit server_default: the ORM
        # default only fires on an ORM insert, so a fresh install that
        # writes a row through raw SQL would NULL-violate without it.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_influxdb_target_name", "influxdb_target", ["name"], unique=True)
    op.create_index("ix_influxdb_target_enabled", "influxdb_target", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_influxdb_target_enabled", table_name="influxdb_target")
    op.drop_index("ix_influxdb_target_name", table_name="influxdb_target")
    op.drop_table("influxdb_target")
