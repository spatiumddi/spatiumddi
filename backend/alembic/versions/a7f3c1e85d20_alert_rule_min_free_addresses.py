"""alert_rule.min_free_addresses (dhcp_pool_exhaustion alert, #339)

Adds the absolute free-address floor for the new ``dhcp_pool_exhaustion``
alert rule type. The rule fires when a dynamic DHCP pool's occupancy
reaches ``threshold_percent`` (already present) OR its free-address count
drops below ``min_free_addresses``. Nullable — only meaningful for the new
rule type; every existing rule keeps NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7f3c1e85d20"
# Chains off the real single head on main (the #336 dns_zone masters
# migration merged via #342) — NOT fe6715916c27, which sits mid-chain.
down_revision: str | None = "c9a1f7e0b234"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("alert_rule", sa.Column("min_free_addresses", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("alert_rule", "min_free_addresses")
