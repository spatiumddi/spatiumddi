"""Logical ownership entities — customer / site / provider (issue #91).

Revision ID: c2a7e4f81b69
Revises: f63d9a8e2c47
Create Date: 2026-05-04 23:00:00

Adds three first-class tables:

* ``customer`` — a logical owner of network resources (soft-deletable).
* ``site`` — a physical location (hierarchical via ``parent_site_id``).
* ``provider`` — an upstream provider / carrier / registrar.

Then wires nullable cross-reference columns onto eight existing tables
so operators can tag who owns / hosts / supplies each resource:

  * ``ip_space.customer_id``
  * ``ip_block.customer_id`` + ``ip_block.site_id``
  * ``subnet.customer_id`` + ``subnet.site_id``
  * ``vrf.customer_id``
  * ``dns_zone.customer_id``
  * ``asn.customer_id`` + ``asn.provider_id``
  * ``network_device.site_id``
  * ``domain.registrar_provider_id`` + ``domain.customer_id``

Every cross-reference column is ``ON DELETE SET NULL`` — a customer /
site / provider deletion never cascades into core IPAM / DNS / DHCP
rows. Operators want to re-tag, not lose data. The matching indexes
keep the per-customer / per-site / per-provider list filters cheap.

The free-form ``Domain.registrar`` text column stays put through this
release. Backfilling its values into freshly auto-created Provider
rows is explicitly deferred (issue #91 "Deferred follow-ups") so we
don't silently mangle operator-curated registrar names that don't
match the eventual canonical Provider.name.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "c2a7e4f81b69"
down_revision: str | None = "f63d9a8e2c47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. customer ────────────────────────────────────────────────────
    op.create_table(
        "customer",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("account_number", sa.String(length=64), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("contact_phone", sa.String(length=64), nullable=True),
        sa.Column("contact_address", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "tags",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "custom_fields",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        # SoftDeleteMixin columns — same shape as IPSpace / IPBlock / Subnet.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deleted_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deletion_batch_id", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_customer_name"),
    )
    op.create_index("ix_customer_status", "customer", ["status"])
    op.create_index("ix_customer_deleted_at", "customer", ["deleted_at"])
    op.create_index("ix_customer_deletion_batch_id", "customer", ["deletion_batch_id"])

    # ── 2. site ────────────────────────────────────────────────────────
    op.create_table(
        "site",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'datacenter'"),
        ),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column(
            "parent_site_id",
            UUID(as_uuid=True),
            sa.ForeignKey("site.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "tags",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
    )
    # ``code`` is unique per parent (incl. NULL parent → top-level
    # namespace). NULLS NOT DISTINCT requires PG 15+.
    op.create_index(
        "ix_site_parent_code_unique",
        "site",
        ["parent_site_id", "code"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_site_kind", "site", ["kind"])
    op.create_index("ix_site_region", "site", ["region"])
    op.create_index("ix_site_parent_site_id", "site", ["parent_site_id"])

    # ── 3. provider ────────────────────────────────────────────────────
    op.create_table(
        "provider",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'transit'"),
        ),
        sa.Column("account_number", sa.String(length=64), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("contact_phone", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "default_asn_id",
            UUID(as_uuid=True),
            sa.ForeignKey("asn.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "tags",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint("name", name="uq_provider_name"),
    )
    op.create_index("ix_provider_kind", "provider", ["kind"])
    op.create_index("ix_provider_default_asn_id", "provider", ["default_asn_id"])

    # ── 4. cross-reference columns on existing tables ─────────────────
    # Helper closure keeps each add-column + FK + index trio compact.
    def _add_owner_fk(table: str, column: str, target: str) -> None:
        op.add_column(table, sa.Column(column, UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_{column}",
            table,
            target,
            [column],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(f"ix_{table}_{column}", table, [column])

    _add_owner_fk("ip_space", "customer_id", "customer")

    _add_owner_fk("ip_block", "customer_id", "customer")
    _add_owner_fk("ip_block", "site_id", "site")

    _add_owner_fk("subnet", "customer_id", "customer")
    _add_owner_fk("subnet", "site_id", "site")

    _add_owner_fk("vrf", "customer_id", "customer")

    _add_owner_fk("dns_zone", "customer_id", "customer")

    _add_owner_fk("asn", "customer_id", "customer")
    _add_owner_fk("asn", "provider_id", "provider")

    _add_owner_fk("network_device", "site_id", "site")

    _add_owner_fk("domain", "customer_id", "customer")
    _add_owner_fk("domain", "registrar_provider_id", "provider")


def downgrade() -> None:
    # Drop cross-reference columns first (reverse order).
    def _drop_owner_fk(table: str, column: str) -> None:
        op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_constraint(f"fk_{table}_{column}", table, type_="foreignkey")
        op.drop_column(table, column)

    _drop_owner_fk("domain", "registrar_provider_id")
    _drop_owner_fk("domain", "customer_id")
    _drop_owner_fk("network_device", "site_id")
    _drop_owner_fk("asn", "provider_id")
    _drop_owner_fk("asn", "customer_id")
    _drop_owner_fk("dns_zone", "customer_id")
    _drop_owner_fk("vrf", "customer_id")
    _drop_owner_fk("subnet", "site_id")
    _drop_owner_fk("subnet", "customer_id")
    _drop_owner_fk("ip_block", "site_id")
    _drop_owner_fk("ip_block", "customer_id")
    _drop_owner_fk("ip_space", "customer_id")

    op.drop_index("ix_provider_default_asn_id", table_name="provider")
    op.drop_index("ix_provider_kind", table_name="provider")
    op.drop_table("provider")

    op.drop_index("ix_site_parent_site_id", table_name="site")
    op.drop_index("ix_site_region", table_name="site")
    op.drop_index("ix_site_kind", table_name="site")
    op.drop_index("ix_site_parent_code_unique", table_name="site")
    op.drop_table("site")

    op.drop_index("ix_customer_deletion_batch_id", table_name="customer")
    op.drop_index("ix_customer_deleted_at", table_name="customer")
    op.drop_index("ix_customer_status", table_name="customer")
    op.drop_table("customer")
