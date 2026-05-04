"""IPAM template classes (issue #26).

Revision ID: f9c1a7e25b83
Revises: f4a6c8b2e571
Create Date: 2026-05-03 12:00:00.000000

Reusable stamp templates that pre-fill tags, custom fields, DNS /
DHCP group assignments, DDNS settings, and an optional sub-subnet
``child_layout`` on block/subnet create. ``applies_to`` locks each
template to one of the two carriers so apply-time semantics are
unambiguous.

``ip_block.applied_template_id`` and ``subnet.applied_template_id``
are nullable back-references SET NULL on template delete so a
"reapply to all instances" sweep can find every row touched by a
template without dropping the carrier when the template is removed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "f9c1a7e25b83"
down_revision: Union[str, None] = "f4a6c8b2e571"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ipam_template",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("applies_to", sa.String(16), nullable=False),
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
        sa.Column("dns_group_id", UUID(as_uuid=True), nullable=True),
        sa.Column("dns_zone_id", sa.Text(), nullable=True),
        sa.Column(
            "dns_additional_zone_ids",
            JSONB(),
            nullable=True,
        ),
        sa.Column("dhcp_group_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "ddns_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "ddns_hostname_policy",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'client_or_generated'"),
        ),
        sa.Column("ddns_domain_override", sa.String(255), nullable=True),
        sa.Column("ddns_ttl", sa.Integer(), nullable=True),
        sa.Column("child_layout", JSONB(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["dns_group_id"],
            ["dns_server_group.id"],
            name="fk_ipam_template_dns_group",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["dhcp_group_id"],
            ["dhcp_server_group.id"],
            name="fk_ipam_template_dhcp_group",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("name", name="uq_ipam_template_name"),
        sa.CheckConstraint(
            "applies_to IN ('block', 'subnet')",
            name="ck_ipam_template_applies_to",
        ),
    )
    op.create_index("ix_ipam_template_applies_to", "ipam_template", ["applies_to"])

    op.add_column(
        "ip_block",
        sa.Column("applied_template_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ip_block_applied_template",
        "ip_block",
        "ipam_template",
        ["applied_template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ip_block_applied_template",
        "ip_block",
        ["applied_template_id"],
        postgresql_where=sa.text("applied_template_id IS NOT NULL"),
    )

    op.add_column(
        "subnet",
        sa.Column("applied_template_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_subnet_applied_template",
        "subnet",
        "ipam_template",
        ["applied_template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_subnet_applied_template",
        "subnet",
        ["applied_template_id"],
        postgresql_where=sa.text("applied_template_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_subnet_applied_template", table_name="subnet")
    op.drop_constraint("fk_subnet_applied_template", "subnet", type_="foreignkey")
    op.drop_column("subnet", "applied_template_id")

    op.drop_index("ix_ip_block_applied_template", table_name="ip_block")
    op.drop_constraint("fk_ip_block_applied_template", "ip_block", type_="foreignkey")
    op.drop_column("ip_block", "applied_template_id")

    op.drop_index("ix_ipam_template_applies_to", table_name="ipam_template")
    op.drop_table("ipam_template")
