"""approvals self-protection lock flag (#62)

Adds ``platform_settings.approvals_protect_controls`` — the opt-in
self-governance lock for the two-person approval workflow. When True,
weakening the approval control plane (disabling the module, disabling /
deleting a policy, lowering a policy's superadmin gate, or turning this
lock off) requires a second superadmin's approval. Additive boolean with
``server_default false`` so every existing install stays exactly as it is.

Revision ID: 5443d2756647
Revises: 2c24fe41a7ed
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "5443d2756647"
down_revision: str | None = "2c24fe41a7ed"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "approvals_protect_controls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "approvals_protect_controls")
