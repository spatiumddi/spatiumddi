"""DHCP PXE / iPXE provisioning profiles (issue #51).

Revision ID: d5b8a3f12e64
Revises: c8a3f1e94d27
Create Date: 2026-05-04 00:00:00.000000

Two new tables plus one column on ``dhcp_scope`` to support
first-class PXE / iPXE provisioning, replacing manual option-67 /
option-66 / DHCP-class stuffing.

* ``dhcp_pxe_profile`` is group-scoped (mirrors how scopes /
  pools / statics live on ``DHCPServerGroup``). One profile
  carries N arch-matches; an operator picks one profile per
  scope. Disabled profiles render no classes.
* ``dhcp_pxe_arch_match`` is the per-arch row. ``priority`` is
  the deterministic tie-breaker — Kea evaluates client-classes
  in declared order; we render in (priority ASC, id ASC) so
  config diffs stay stable across runs.
* ``dhcp_scope.pxe_profile_id`` is nullable + ON DELETE SET NULL
  so deleting a profile doesn't cascade-trash the scope. The
  scope just stops emitting PXE classes on the next bundle.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "d5b8a3f12e64"
down_revision: Union[str, None] = "c8a3f1e94d27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dhcp_pxe_profile",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_server", sa.String(45), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("group_id", "name", name="uq_dhcp_pxe_profile_group_name"),
    )

    op.create_table(
        "dhcp_pxe_arch_match",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dhcp_pxe_profile.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "match_kind",
            sa.String(20),
            nullable=False,
            server_default="first_stage",
        ),
        sa.Column("vendor_class_match", sa.String(255), nullable=True),
        sa.Column("arch_codes", JSONB(), nullable=True),
        sa.Column("boot_filename", sa.String(512), nullable=False),
        sa.Column("boot_file_url_v6", sa.String(512), nullable=True),
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
    op.create_index(
        "ix_dhcp_pxe_arch_match_profile_priority",
        "dhcp_pxe_arch_match",
        ["profile_id", "priority"],
    )

    op.add_column(
        "dhcp_scope",
        sa.Column(
            "pxe_profile_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dhcp_pxe_profile.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_dhcp_scope_pxe_profile",
        "dhcp_scope",
        ["pxe_profile_id"],
        postgresql_where=sa.text("pxe_profile_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_dhcp_scope_pxe_profile", table_name="dhcp_scope")
    op.drop_column("dhcp_scope", "pxe_profile_id")
    op.drop_index(
        "ix_dhcp_pxe_arch_match_profile_priority", table_name="dhcp_pxe_arch_match"
    )
    op.drop_table("dhcp_pxe_arch_match")
    op.drop_table("dhcp_pxe_profile")
