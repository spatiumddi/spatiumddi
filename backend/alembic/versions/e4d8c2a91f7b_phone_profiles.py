"""VoIP phone profiles + scope attach (issue #112 phase 1).

Revision ID: e4d8c2a91f7b
Revises: d9e4c12a7f85
Create Date: 2026-05-07 12:00:00

Adds two tables:

- ``dhcp_phone_profile`` — group-scoped reusable VoIP phone
  provisioning profile. Carries a vendor-class-id substring match
  + an ``option_set`` JSONB list of DHCP options to deliver. The Kea
  driver renders one client-class per profile.
- ``dhcp_phone_profile_scope`` — M:N join. Same profile can attach
  to multiple voice VLANs without duplication; same scope can carry
  multiple vendor profiles (Polycom + Yealink + Cisco SPA on one
  voice subnet).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "e4d8c2a91f7b"
down_revision: str | None = "d9e4c12a7f85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dhcp_phone_profile",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "description", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("vendor", sa.String(64), nullable=True),
        sa.Column("vendor_class_match", sa.String(255), nullable=True),
        sa.Column(
            "option_set",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "tags", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("group_id", "name", name="uq_dhcp_phone_profile_group_name"),
    )
    op.create_index(
        "ix_dhcp_phone_profile_group_id",
        "dhcp_phone_profile",
        ["group_id"],
    )

    op.create_table(
        "dhcp_phone_profile_scope",
        sa.Column(
            "profile_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dhcp_phone_profile.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "scope_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dhcp_scope.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("dhcp_phone_profile_scope")
    op.drop_index("ix_dhcp_phone_profile_group_id", table_name="dhcp_phone_profile")
    op.drop_table("dhcp_phone_profile")
