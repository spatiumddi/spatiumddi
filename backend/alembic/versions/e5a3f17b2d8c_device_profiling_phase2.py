"""device profiling phase 2 — passive DHCP fingerprinting + fingerbank

New ``dhcp_fingerprint`` table (one row per MAC) carrying the raw
DHCP option-55 / option-60 / option-77 / client-id signature plus a
denormalised fingerbank lookup result. The agent's scapy sniffer
ships fingerprints to the control plane; a Celery task hits
fingerbank for enrichment and stamps the resolved
``device_type`` / ``device_class`` / ``device_manufacturer`` onto
matching ``ip_address`` rows for fast list-rendering.

Revision ID: e5a3f17b2d8c
Revises: d4f2a86c5b91
Create Date: 2026-04-30 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e5a3f17b2d8c"
down_revision: Union[str, None] = "d4f2a86c5b91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dhcp_fingerprint",
        sa.Column("mac_address", postgresql.MACADDR(), nullable=False),
        sa.Column("option_55", sa.Text(), nullable=True),
        sa.Column("option_60", sa.Text(), nullable=True),
        sa.Column("option_77", sa.Text(), nullable=True),
        sa.Column("client_id", sa.Text(), nullable=True),
        sa.Column("fingerbank_device_id", sa.Integer(), nullable=True),
        sa.Column("fingerbank_device_name", sa.String(length=255), nullable=True),
        sa.Column("fingerbank_device_class", sa.String(length=100), nullable=True),
        sa.Column("fingerbank_manufacturer", sa.String(length=100), nullable=True),
        sa.Column("fingerbank_score", sa.Integer(), nullable=True),
        sa.Column(
            "fingerbank_last_lookup_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("fingerbank_last_error", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
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
        sa.PrimaryKeyConstraint("mac_address"),
    )
    op.create_index(
        "ix_dhcp_fingerprint_last_seen_at",
        "dhcp_fingerprint",
        ["last_seen_at"],
    )

    op.add_column(
        "ip_address",
        sa.Column("device_type", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "ip_address",
        sa.Column("device_class", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "ip_address",
        sa.Column("device_manufacturer", sa.String(length=100), nullable=True),
    )

    # Fernet-encrypted fingerbank API key on the singleton platform_settings
    # row. Stored as bytea to mirror how other secret fields will land when
    # we tighten secrets at rest across the board.
    op.add_column(
        "platform_settings",
        sa.Column(
            "fingerbank_api_key_encrypted",
            sa.LargeBinary(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "fingerbank_api_key_encrypted")
    op.drop_column("ip_address", "device_manufacturer")
    op.drop_column("ip_address", "device_class")
    op.drop_column("ip_address", "device_type")
    op.drop_index("ix_dhcp_fingerprint_last_seen_at", table_name="dhcp_fingerprint")
    op.drop_table("dhcp_fingerprint")
