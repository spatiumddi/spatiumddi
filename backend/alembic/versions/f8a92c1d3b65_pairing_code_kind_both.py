"""Issue #169 — Extend pairing_code.deployment_kind to allow 'both'

Operator follow-up after the Phase 1+2 cut: a single agent appliance
can run BIND9 + Kea simultaneously (one box, both services). The
original migration ``e2c91d5f7a48_appliance_pairing_codes`` pinned
the kind CHECK to ``('dns', 'dhcp')`` only, so this widens it.

Postgres can't ALTER a CHECK in place — we drop + re-add. Cheap on
this small table.

Server-group pre-assignment for ``kind='both'`` rows is deliberately
NOT split into per-service columns here. A combined agent's groups
are configured per-service after registration through the existing
DNS / DHCP server-group UI; the natural place for at-pairing-time
two-group selection is #170 (control-plane-driven role assignment),
not this code-shortener.

Revision ID: f8a92c1d3b65
Revises: e2c91d5f7a48
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op

revision: str = "f8a92c1d3b65"
down_revision: str | None = "e2c91d5f7a48"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE pairing_code DROP CONSTRAINT pairing_code_deployment_kind_chk")
    op.execute(
        "ALTER TABLE pairing_code ADD CONSTRAINT pairing_code_deployment_kind_chk "
        "CHECK (deployment_kind IN ('dns', 'dhcp', 'both'))"
    )


def downgrade() -> None:
    # Revert to the narrower set. Any 'both' rows that exist would
    # block the constraint — clear them first so the downgrade is
    # safe to re-run.
    op.execute("DELETE FROM pairing_code WHERE deployment_kind = 'both'")
    op.execute("ALTER TABLE pairing_code DROP CONSTRAINT pairing_code_deployment_kind_chk")
    op.execute(
        "ALTER TABLE pairing_code ADD CONSTRAINT pairing_code_deployment_kind_chk "
        "CHECK (deployment_kind IN ('dns', 'dhcp'))"
    )
