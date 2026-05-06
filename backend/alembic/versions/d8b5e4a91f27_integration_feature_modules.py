"""seed integration feature_module rows + backfill from PlatformSettings

The previous migration (``c4f7a1d3e589``) created ``feature_module``
and seeded the Network / AI / Compliance / Tools entries. This one
adds the four integration ids — ``integrations.{kubernetes,docker,
proxmox,tailscale}`` — and backfills each row's ``enabled`` from the
existing ``platform_settings.integration_*_enabled`` columns so
operators who already had an integration on don't lose it on upgrade.

Catalog default for these is ``False`` (each integration needs
operator-supplied credentials before it does anything useful), but
existing on-toggles take precedence over the catalog default — that's
what the backfill is for.

Revision ID: d8b5e4a91f27
Revises: c4f7a1d3e589
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d8b5e4a91f27"
down_revision = "c4f7a1d3e589"
branch_labels = None
depends_on = None


# (feature_module_id, platform_settings_column).
_INTEGRATION_MIRROR: tuple[tuple[str, str], ...] = (
    ("integrations.kubernetes", "integration_kubernetes_enabled"),
    ("integrations.docker", "integration_docker_enabled"),
    ("integrations.proxmox", "integration_proxmox_enabled"),
    ("integrations.tailscale", "integration_tailscale_enabled"),
)


def upgrade() -> None:
    bind = op.get_bind()
    settings_row = bind.execute(
        sa.text("SELECT * FROM platform_settings WHERE id = 1")
    ).mappings().first()

    for module_id, column in _INTEGRATION_MIRROR:
        existing = settings_row.get(column) if settings_row else False
        # Insert with the existing toggle state so previously-enabled
        # integrations stay enabled after the upgrade. Skip if a row
        # already exists for this id (idempotent — safe to re-run).
        op.execute(
            sa.text(
                "INSERT INTO feature_module (id, enabled) "
                "VALUES (:id, :enabled) ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=module_id, enabled=bool(existing))
        )


def downgrade() -> None:
    for module_id, _ in _INTEGRATION_MIRROR:
        op.execute(
            sa.text("DELETE FROM feature_module WHERE id = :id").bindparams(id=module_id)
        )
