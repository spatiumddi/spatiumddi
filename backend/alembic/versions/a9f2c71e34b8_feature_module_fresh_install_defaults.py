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
On a fresh install only, delete every ``feature_module`` row that no
operator has touched. With no row, the catalog default applies — which
is the whole point, and makes a row mean what the service docstring
always claimed it meant: "an operator changed this".

An EXISTING install keeps every row and is bit-for-bit unchanged. That
is deliberate: an operator who has been running E911 or Conformity must
not lose it to an upgrade they did not ask for. It also means an
existing install pins the defaults it was installed with, which is the
correct reading of "an upgrade never silently changes behaviour".

WHY ``audit_log`` AND NOT ``"user"``
------------------------------------
The obvious test is "no users yet". It is wrong, and wrong in the
direction that fails silently.

``app.main._seed_default_admin`` inserts the default admin at API
lifespan start, and on Docker Compose the ``api`` service has only ever
waited for postgres and redis — not for ``migrate`` to finish. So on a
cold ``docker compose up -d`` the API can insert that admin while these
291 revisions are still running: ``"user"`` is created within the first
handful of them, and this one is last. The row count would then be 1,
this migration would decide the install was an upgrade, skip, and #1069
would quietly do nothing at all on exactly the installs it is aimed at.
(That ordering is now pinned by a ``service_completed_successfully``
dependency in both compose files, but a migration must not depend on a
deployment detail it cannot see — an operator running ``docker compose
up -d api`` by hand, or an older compose file still in the field, gets
no such guarantee.)

``audit_log`` has neither problem. Non-negotiable #4 puts a row there
for every mutation before the response is returned, and logging in
writes one too — so a non-empty table means a human has used this
install. Nothing on the startup path writes to it: the admin seed, the
builtin roles, the BGP communities, the conformity policies and the
alert rules are all system seeds, not audited mutations. So a racing
API cannot manufacture the evidence, and an empty ``audit_log`` means
nobody has configured anything and there is nothing to preserve.

``updated_by_id IS NULL`` narrows the delete on top of that: a module an
operator has actually toggled carries their id. On a genuinely fresh
install nothing can have been toggled, so it is redundant — which is
the point of keeping it. If the freshness test is ever wrong, the blast
radius is still only rows nobody edited.

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

import logging

import sqlalchemy as sa

from alembic import op

revision: str = "a9f2c71e34b8"
down_revision: str | None = "c1f4a90e7d63"
branch_labels: str | None = None
depends_on: str | None = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()

    # EXISTS, not count(*) — audit_log is one of the largest tables on a
    # long-lived install and the question is only "any row at all".
    used = conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM audit_log)")).scalar_one()
    if used:
        logger.info(
            "#1069: audit_log is non-empty, so this is an existing install — "
            "leaving every feature_module row alone. The modules enabled here "
            "stay exactly as they are."
        )
        return

    deleted = conn.execute(
        sa.text("DELETE FROM feature_module WHERE updated_by_id IS NULL")
    ).rowcount
    logger.info(
        "#1069: fresh install (audit_log empty) — deleted %s pristine feature_module "
        "seed row(s); the catalog's default_enabled now governs.",
        deleted,
    )


def downgrade() -> None:
    """Intentionally empty — see the module docstring.

    The old code's catalog carries the old defaults, so an absent row
    resolves to the old behaviour on its own. Re-inserting rows here
    would additionally re-create the #1069 bug on the way back down.
    """
