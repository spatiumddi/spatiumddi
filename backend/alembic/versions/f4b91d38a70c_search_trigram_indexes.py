"""Trigram indexes for global search (issue #879)

Global search matches with ``ILIKE '%term%'``. A leading wildcard makes a
B-tree index unusable, so every keystroke was a sequential scan of every
searched table — and search fans out across all of them at once.

``pg_trgm`` GIN indexes fix that: they support leading-wildcard ILIKE
directly. Two caveats worth knowing before reading an EXPLAIN and
concluding this migration did nothing:

* A pattern shorter than three characters produces no full trigram, so
  1–2 character queries still seq-scan. That is fine — the palette is
  debounced and those queries match nearly everything anyway.
* GIN is chosen over GiST deliberately: lookups are much faster, and the
  slower build and larger index matter far less for a workload that reads
  on every keystroke and writes on operator action.

**Only the tables where a scan actually hurts are indexed.** ``ip_address``
and ``dns_record`` reach millions of rows on a real install; ``ip_space``,
``dns_view``, ``site`` and friends hold tens to hundreds, where a seq scan
is already the fastest plan and a GIN index would be pure write
amplification. Adding one later is a one-line migration.

Extension creation is attempted inside a SAVEPOINT. ``pg_trgm`` has been a
*trusted* extension since PostgreSQL 13, so a database owner can create it
without superuser — but a locked-down managed instance may still refuse. If
it does, this migration logs and skips the indexes rather than failing:
search degrades to the sequential scans it already used, which is a slow
search, not a broken upgrade. Doing that without the SAVEPOINT would leave
the connection in "current transaction is aborted" and take down every
later statement in the migration with it.

Revision ID: f4b91d38a70c
Revises: c8a3f207e51b
Create Date: 2026-08-22

"""

from __future__ import annotations

import structlog
from alembic import op

revision = "f4b91d38a70c"
down_revision = "c8a3f207e51b"
branch_labels = None
depends_on = None

logger = structlog.get_logger(__name__)

# (index name, table, column). Ordinary column indexes.
_TRGM_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_trgm_ip_address_hostname", "ip_address", "hostname"),
    ("ix_trgm_ip_address_description", "ip_address", "description"),
    ("ix_trgm_dns_record_fqdn", "dns_record", "fqdn"),
    ("ix_trgm_dns_record_value", "dns_record", "value"),
    ("ix_trgm_dns_zone_name", "dns_zone", "name"),
    ("ix_trgm_subnet_name", "subnet", "name"),
    ("ix_trgm_subnet_description", "subnet", "description"),
    ("ix_trgm_dhcp_static_hostname", "dhcp_static_assignment", "hostname"),
)
# Deliberately not indexed: ``dhcp_static_assignment.ip_address`` and
# ``network_device.ip_address``. Both are ``inet``, which ``gin_trgm_ops``
# rejects outright, so they would need an index over ``CAST(… AS text)``
# spelled exactly as the query spells it. Neither table is large enough for
# that fragility to buy anything — a reservation list is bounded by the
# scopes an operator maintains, not by address space.

# The MAC lookup normalises separators away on both sides, so the index has
# to be over that same expression — the planner matches expression indexes
# by comparing the parsed expression, and any divergence silently costs the
# index rather than raising. The live query builds this string from
# ``app.services.search.providers._mac_normalized_sql``; it is spelled out
# again here rather than imported, because a migration is a historical
# record that has to keep running after the code it was written against has
# moved on. ``test_search.py::test_mac_index_expression_matches_the_query``
# fails if the two ever diverge, which is the right place for that check:
# it catches drift in CI instead of in an EXPLAIN nobody runs.
_MAC_NORMALIZE_SQL = (
    "REPLACE(REPLACE(REPLACE(CAST(mac_address AS text), ':', ''), '-', ''), '.', '')"
)
_MAC_INDEX = "ix_trgm_ip_address_mac_normalized"


def _ensure_pg_trgm() -> bool:
    """Create the extension if needed. Returns whether it is usable."""
    bind = op.get_bind()
    existing = bind.exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").scalar()
    if existing:
        return True
    try:
        with bind.begin_nested():
            bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the upgrade
        logger.warning(
            "search_trgm_extension_unavailable",
            error=str(exc),
            detail=(
                "Global search will use sequential scans. Grant the database "
                "owner rights to CREATE EXTENSION pg_trgm and re-run this "
                "migration to enable the indexes."
            ),
        )
        return False
    return True


def upgrade() -> None:
    if not _ensure_pg_trgm():
        return

    for name, table, column in _TRGM_INDEXES:
        op.create_index(
            name,
            table,
            [column],
            unique=False,
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )

    op.execute(
        f"CREATE INDEX {_MAC_INDEX} ON ip_address "
        f"USING gin (({_MAC_NORMALIZE_SQL}) gin_trgm_ops)"
    )


def downgrade() -> None:
    # IF EXISTS throughout: upgrade() legitimately creates none of these
    # when the extension is unavailable, so an unconditional DROP would make
    # the downgrade fail on exactly the installs that took that path.
    op.execute(f"DROP INDEX IF EXISTS {_MAC_INDEX}")
    for name, _table, _column in _TRGM_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    # The extension is deliberately left in place — something else may have
    # come to depend on it, and dropping it would cascade away their indexes.
