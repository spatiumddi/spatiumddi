"""Rename appliance_slot_image -> appliance_upgrade_image (#199).

Operator-facing rename: the uploaded/imported ``.raw.xz`` artifact is an
"upgrade image". "Slot" stays the name of the lower-level A/B dd
mechanism (``services/appliance/slot.py``, the host runners, the
``desired_slot_image_url`` desired-state columns + the on-disk
``/var/lib/spatiumddi/slot-images`` storage dir — all unchanged).

This is a pure ``rename_table`` so existing rows + the data they
reference are preserved verbatim. Postgres carries the PK / UNIQUE
(sha256) / FK constraints + indexes across the rename; their internal
names keep the historical ``appliance_slot_image`` prefix, which is
cosmetic and never queried by name. No column changes.

Rolling-upgrade note (#296): ``scripts/lint_migrations.py`` flags
``rename_table`` because a bare rename isn't expand/contract-safe in
the general case. It's baselined here (``migrations_lint_baseline.txt``)
because the rename is low-risk: ``appliance_upgrade_image`` is a
transient, superadmin-only table (operator-staged upgrade images), and
the multi-node control-plane rolling upgrade orchestrator (#296) runs
the migrate Job *after* the api/frontend/worker pod rollout completes —
so no old-schema pod is still serving when the table is renamed.

Revision ID: c3f7a1d9b486
Revises: f1a4c7b2e9d6
Create Date: 2026-06-11
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "c3f7a1d9b486"
down_revision = "f1a4c7b2e9d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("appliance_slot_image", "appliance_upgrade_image")


def downgrade() -> None:
    op.rename_table("appliance_upgrade_image", "appliance_slot_image")
