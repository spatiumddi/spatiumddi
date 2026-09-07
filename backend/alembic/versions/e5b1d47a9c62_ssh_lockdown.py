"""ssh_lockdown — make the SSH source-CIDR allowlist enforceable (#1009)

The allowlist has shipped since #157 and, on the default port, enforced
nothing: the management floor opens 22 unconditionally *before* the scoped
drop-in in the include glob, and nftables is first-match-wins. Retiring that
floor removes what ``docs/design/FLEET_FIREWALL.md`` §6.1 calls the
irreducible recovery channel, so it is an explicit operator decision rather
than a side effect of typing a CIDR.

Defaults FALSE, which is deliberately a no-op for every existing install:
an operator who configured the list while it was inert does not get their
SSH tightened by an upgrade they did not ask for.

Revision ID: e5b1d47a9c62
Revises: c93f1a72e408
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa

revision = "e5b1d47a9c62"
down_revision = "c93f1a72e408"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "ssh_lockdown",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "ssh_lockdown")
