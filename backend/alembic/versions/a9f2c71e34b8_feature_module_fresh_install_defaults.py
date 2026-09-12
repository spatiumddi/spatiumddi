"""#1069 — let the catalog's ``default_enabled`` govern a fresh install.

Data-only. No schema change, no new table, no new column.

WHY THIS EXISTS
---------------
``app.services.feature_modules`` resolves a module as "DB row if one
exists, else the catalog's ``default_enabled``". Non-negotiable #14 also
told every module's migration to SEED a row at its shipped default — so
a row always existed, and the catalog default was dead code on every
install. Flipping a ``default_enabled`` in the catalog therefore changed
NOTHING, fresh installs included, because a fresh install runs exactly
the same seed migrations an upgrade does.

#1069 revises which modules ship enabled (37 of 53 → 14 of 53). For that
to reach anybody, the pristine seed rows have to go.

WHAT IT DOES
------------
On a FRESH install only, delete every ``feature_module`` row that no
operator has touched. With no row, the catalog default applies — which
is the whole point, and makes a row mean what the service docstring
always claimed it meant: "an operator changed this".

"Fresh" is ``"user"`` being empty. The default admin is created by the
API's lifespan startup (``app.main._seed_default_admin``), which cannot
have run before ``alembic upgrade`` on a fresh install — the compose /
k8s / appliance paths all migrate first. So at this point:

    no users  ⟺  the application has never started  ⟺  fresh install

and on a fresh install every ``feature_module`` row is necessarily a
seed, because there has been nobody to log in and toggle one.
``updated_by_id IS NULL`` is therefore redundant — it is in the WHERE
clause anyway as a safety net, so that if the freshness test is ever
wrong the blast radius is still only rows nobody edited.

An EXISTING install keeps every row and is bit-for-bit unchanged. That
is deliberate: an operator who has been running E911 or Conformity must
not lose it to an upgrade they did not ask for. It also means an
existing install pins the defaults it was installed with, which is the
correct reading of "an upgrade never silently changes behaviour".

DOWNGRADE IS A GENUINE NO-OP
----------------------------
Not laziness — the rows do not need restoring. On an existing install
nothing was deleted. On a fresh install the rows are gone, but
downgrading the code restores the OLD catalog, whose ``default_enabled``
for those modules is ``True``; with no rows present that old default
applies, which is exactly the state the old version would have reached.

Revision ID: a9f2c71e34b8
Revises: c1f4a90e7d63
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a9f2c71e34b8"
down_revision: str | None = "c1f4a90e7d63"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # Quoted — "user" is a reserved word in Postgres.
    users = conn.execute(sa.text('SELECT count(*) FROM "user"')).scalar_one()
    if users:
        # Existing install: leave every row exactly as it is.
        return

    conn.execute(sa.text("DELETE FROM feature_module WHERE updated_by_id IS NULL"))


def downgrade() -> None:
    """Intentionally empty — see the module docstring.

    The old code's catalog carries the old defaults, so an absent row
    resolves to the old behaviour on its own. Re-inserting rows here
    would additionally re-create the #1069 bug on the way back down.
    """
