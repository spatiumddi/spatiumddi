"""Fragile-device "do not probe" flag on the IPAM hierarchy (#722)

Adds ``do_not_probe`` + ``do_not_probe_reason`` to ``ip_space``,
``ip_block`` and ``subnet``.

The flag suppresses SpatiumDDI's *own* active probes against anything
inside the marked scope — the #23 discovery ping/ARP sweep, ``tools.nmap``
(including the per-subnet auto-profile-on-lease trigger) and the
``tools.network`` reachability tools. Medical- and OT-device vendors
instruct sites not to scan their VLANs, and fragile IP stacks genuinely
fault under a sweep; until now there was no way for an operator to say so.

Two deliberate departures from the DDNS columns this otherwise mirrors:

* **No ``*_inherit_settings`` companion column.** DDNS resolves to the
  first non-inheriting ancestor, which lets a descendant override its
  parent. A safety constraint must not work that way, so this flag ORs
  downward: any level saying "do not probe" wins and nothing below it
  can un-say it. That is a property of the resolver
  (``app.services.ipam.probe_policy``), not of the schema — which is why
  the columns themselves are plain and this note exists.
* **No feature-module gate.** It is a constraint on our own behaviour
  and a correctness fix to shipped features, so it ships on by default
  for every install.

Also widens no enum — ``bmc`` joins the ``IP_ROLES`` frozenset in the
same issue, but that set is validated in the API layer and has no
database representation, so it needs no migration.

Revision ID: b1e7c04a93df
Revises: a4f81c26b9e3
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b1e7c04a93df"
down_revision: str | None = "a4f81c26b9e3"
branch_labels: str | None = None
depends_on: str | None = None

# The three levels of the IPAM containment chain the flag rides.
_TABLES: tuple[str, ...] = ("ip_space", "ip_block", "subnet")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "do_not_probe",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "do_not_probe_reason",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            ),
        )

    # Partial index on the subnet level only. The discovery dispatcher and
    # the conformity check both ask "is this subnet suppressed", and in a
    # real estate the flagged rows are a small minority — so a partial
    # index is tiny and turns those into index scans. ip_space / ip_block
    # are walked by primary key from a known child, never scanned by flag,
    # so an index there would only cost write throughput.
    op.create_index(
        "ix_subnet_do_not_probe",
        "subnet",
        ["do_not_probe"],
        postgresql_where=sa.text("do_not_probe"),
    )


def downgrade() -> None:
    op.drop_index("ix_subnet_do_not_probe", table_name="subnet")
    for table in _TABLES:
        op.drop_column(table, "do_not_probe_reason")
        op.drop_column(table, "do_not_probe")
