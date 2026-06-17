"""appliance APT host-config columns + apt_state (#155)

Adds the ``platform_settings.apt_*`` block (opt-in managed APT sources /
proxy / GPG keys / private-mirror auth) and ``appliance.apt_state`` (the
per-host validate+swap status the Fleet view surfaces). ``apt_sources``
seeds the Debian 13 (trixie) defaults so enabling management starts from
the working repos rather than an empty sources.list.

Revision ID: e1a4b8c92f3d
Revises: d4e9f2a7c1b8
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1a4b8c92f3d"
down_revision: str | None = "d4e9f2a7c1b8"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen snapshot of the Debian 13 (trixie) seed — inlined (not imported
# from app.models) so this migration stays an immutable historical record
# even if the model's DEFAULT_APT_SOURCES later changes. Kept in sync with
# ``app.models.settings.DEFAULT_APT_SOURCES`` at authoring time.
_SOURCES_DEFAULT = (
    '[{"name": "Debian trixie", "uri": "http://deb.debian.org/debian", '
    '"suites": "trixie", "components": "main contrib non-free-firmware", '
    '"signed_by_key_id": "", "enabled": true}, '
    '{"name": "Debian trixie-updates", "uri": "http://deb.debian.org/debian", '
    '"suites": "trixie-updates", "components": "main contrib non-free-firmware", '
    '"signed_by_key_id": "", "enabled": true}, '
    '{"name": "Debian Security", "uri": "http://security.debian.org/debian-security", '
    '"suites": "trixie-security", "components": "main contrib non-free-firmware", '
    '"signed_by_key_id": "", "enabled": true}]'
)


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column("apt_managed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(f"'{_SOURCES_DEFAULT}'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_gpg_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column("apt_proxy_http", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "platform_settings",
        sa.Column("apt_proxy_https", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "platform_settings",
        sa.Column("apt_proxy_no_proxy", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_auth",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_unattended_upgrades_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    op.add_column(
        "appliance",
        sa.Column("apt_state", sa.String(length=24), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "apt_state")
    op.drop_column("platform_settings", "apt_unattended_upgrades_enabled")
    op.drop_column("platform_settings", "apt_auth")
    op.drop_column("platform_settings", "apt_proxy_no_proxy")
    op.drop_column("platform_settings", "apt_proxy_https")
    op.drop_column("platform_settings", "apt_proxy_http")
    op.drop_column("platform_settings", "apt_gpg_keys")
    op.drop_column("platform_settings", "apt_sources")
    op.drop_column("platform_settings", "apt_managed")
