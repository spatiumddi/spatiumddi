"""appliance unattended-upgrades policy columns (#164)

Adds the ``platform_settings.apt_unattended_*`` policy block (the WHEN/HOW of
auto-applying updates) alongside the existing ``apt_unattended_upgrades_enabled``
master toggle (#155). These render /etc/apt/apt.conf.d/50unattended-upgrades and
apply orthogonally to ``apt_managed`` (the WHERE — #155's sources management), so
an operator can set a reboot policy without taking over apt sources.

``apt_unattended_origins`` seeds the security-only locked-down default so a fresh
install matches Debian's out-of-the-box unattended behaviour.

Revision ID: c9d4a1e8b672
Revises: b1f7c3a92e04
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9d4a1e8b672"
down_revision: str | None = "b1f7c3a92e04"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen snapshot of the model's DEFAULT_UNATTENDED_ORIGINS (inlined so the
# migration stays an immutable historical record). apt expands ${distro_id}
# / ${distro_codename} at runtime.
_ORIGINS_DEFAULT = '["${distro_id}:${distro_codename}-security"]'


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_unattended_origins",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(f"'{_ORIGINS_DEFAULT}'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_unattended_blocklist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_unattended_automatic_reboot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "apt_unattended_reboot_time",
            sa.String(length=5),
            nullable=False,
            server_default=sa.text("'02:00'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "apt_unattended_reboot_time")
    op.drop_column("platform_settings", "apt_unattended_automatic_reboot")
    op.drop_column("platform_settings", "apt_unattended_blocklist")
    op.drop_column("platform_settings", "apt_unattended_origins")
