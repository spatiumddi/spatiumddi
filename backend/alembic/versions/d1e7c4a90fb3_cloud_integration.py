"""cloud integration: cloud_endpoint table + cloud_endpoint_id FKs + settings + module

Revision ID: d1e7c4a90fb3
Revises: c7f1a3e58b94
Create Date: 2026-05-30 18:00:00.000000

Cloud integration (issue #37, Part A — IPAM infrastructure mirror).

* ``cloud_endpoint`` — one row per connected public-cloud account
  (AWS / Azure / GCP). Fernet-encrypted ``credentials_encrypted`` blob +
  non-secret ``provider_config`` JSONB (subscription/project ids) +
  ``regions`` allow-list. Bound to an IPAM space (RESTRICT) + optional
  public space (SET NULL) + optional DNS group (SET NULL).
* ``cloud_endpoint_id`` FK (ON DELETE CASCADE, indexed) on ``ip_block`` /
  ``subnet`` / ``ip_address`` so removing an endpoint sweeps every
  mirrored row atomically — same contract every other integration uses.
* ``platform_settings.integration_cloud_enabled`` — the Celery-beat kill
  switch kept in lock-step with the ``integrations.cloud`` feature
  module by the toggle endpoint.
* ``feature_module`` seed (``integrations.cloud``, disabled) — operator
  opts in; matches ``ModuleSpec.default_enabled=False``.

Part B (Cloud DNS as a first-class driver) needs no schema: cloud DNS
drivers reuse the existing ``dns_server.driver`` string + the
``dns_server.credentials_encrypted`` column + the ``dns_zone`` /
``dns_record`` ``import_source`` / ``imported_at`` provenance columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1e7c4a90fb3"
down_revision: str | None = "c7f1a3e58b94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FK_TABLES = ("ip_block", "subnet", "ip_address")


def upgrade() -> None:
    op.create_table(
        "cloud_endpoint",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column(
            "credentials_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column(
            "provider_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "regions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("ipam_space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_space_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dns_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "mirror_load_balancers", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "mirror_stopped_instances",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("provider_account_id", sa.String(length=255), nullable=True),
        sa.Column("network_count", sa.Integer(), nullable=True),
        sa.Column("instance_count", sa.Integer(), nullable=True),
        sa.Column("last_discovery", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["ipam_space_id"], ["ip_space.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["public_space_id"], ["ip_space.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dns_group_id"], ["dns_server_group.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cloud_endpoint_name", "cloud_endpoint", ["name"], unique=True)

    # cloud_endpoint_id provenance FK on the three IPAM row types.
    for table in _FK_TABLES:
        op.add_column(
            table,
            sa.Column("cloud_endpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table}_cloud_endpoint_id",
            table,
            "cloud_endpoint",
            ["cloud_endpoint_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(
            f"ix_{table}_cloud_endpoint_id",
            table,
            ["cloud_endpoint_id"],
        )

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_cloud_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── feature_module seed (disabled; operator opts in) ────────────────
    op.execute(sa.text("""
            INSERT INTO feature_module (id, enabled)
            VALUES ('integrations.cloud', FALSE)
            ON CONFLICT (id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'integrations.cloud'"))
    op.drop_column("platform_settings", "integration_cloud_enabled")
    for table in _FK_TABLES:
        op.drop_index(f"ix_{table}_cloud_endpoint_id", table_name=table)
        op.drop_constraint(f"fk_{table}_cloud_endpoint_id", table, type_="foreignkey")
        op.drop_column(table, "cloud_endpoint_id")
    op.drop_index("ix_cloud_endpoint_name", table_name="cloud_endpoint")
    op.drop_table("cloud_endpoint")
