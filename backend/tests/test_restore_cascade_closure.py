"""#781 — selective restore must not delete what CASCADE reaches.

``TRUNCATE … CASCADE`` empties every table holding a foreign key into a
truncated one, transitively. Selective restore truncated the selected
sections' tables that way and then ``pg_restore``'d **only those
tables**, so everything else CASCADE touched was silently deleted.

The scale is what makes it serious rather than a footnote. Measured
against the shipped catalog on 2026-08-02: selecting ``auth`` alone
cascades into 130 tables and refills 11. "Restore just my users" emptied
most of the database.

``cascade_closure`` computes that reach from mapped metadata — the FK
graph is what Postgres actually walks, and any hand-maintained copy is
one migration away from being wrong — and the restore path now truncates
AND restores the closure, so the operation ends consistent with the
archive for everything it touched.

Note this is deliberately *wider* than the operator ticked. The
alternative was never "narrower"; it was "emptied".
"""

from __future__ import annotations

from app.models.base import Base
from app.services.backup.sections import (
    SECTIONS,
    all_section_tables,
    cascade_closure,
    tables_for_sections,
)


def _dependents() -> dict[str, set[str]]:
    """parent -> tables that FK into it, straight from metadata."""
    out: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent != name:
                out.setdefault(parent, set()).add(name)
    return out


def test_closure_includes_the_input() -> None:
    """Callers hand the result straight to truncate + pg_restore, so the
    selection itself has to survive the round trip."""
    seed = {"dns_zone", "dns_record"}
    assert seed <= cascade_closure(seed)


def test_closure_is_empty_beyond_input_for_a_leaf_table() -> None:
    """Guard against a closure that just returns everything."""
    deps = _dependents()
    leaves = [t for t in Base.metadata.tables if not deps.get(t)]
    assert leaves, "expected at least one table nothing references"
    assert cascade_closure({leaves[0]}) == {leaves[0]}


def test_closure_follows_a_direct_foreign_key() -> None:
    deps = _dependents()
    parent = next(p for p, kids in deps.items() if kids)
    child = next(iter(deps[parent]))
    assert child in cascade_closure({parent})


def test_closure_is_transitive() -> None:
    """A -> B -> C must pull C in when only A is selected."""
    deps = _dependents()
    for parent, kids in deps.items():
        for kid in kids:
            grandkids = deps.get(kid, set()) - {parent, kid}
            if grandkids:
                got = cascade_closure({parent})
                assert kid in got
                assert grandkids <= got, f"{parent} -> {kid} -> {grandkids}"
                return
    raise AssertionError("expected a two-hop FK chain somewhere in the schema")


def test_closure_terminates_on_a_reference_cycle() -> None:
    """The schema has known FK cycles (asn / dns_record / ip_address /
    nmap_scan / provider — conftest warns about them). A naive walk
    would spin forever, so pin that it doesn't."""
    assert cascade_closure({"ip_address"})  # completes at all
    assert cascade_closure({"asn", "dns_record", "ip_address"})


def test_every_section_closure_is_a_superset_of_its_own_tables() -> None:
    for section in SECTIONS:
        own = set(tables_for_sections([section.key]))
        assert own <= cascade_closure(own), section.key


def test_the_auth_section_is_the_motivating_case() -> None:
    """The regression this exists for.

    ``auth`` owns a handful of tables but ``user`` is referenced from all
    over, so restoring it used to empty most of the install. Assert the
    closure is materially bigger than the selection — if this ever stops
    being true the FK graph changed shape and the fix's premise deserves
    a re-read.
    """
    own = set(tables_for_sections(["auth"]))
    closure = cascade_closure(own)
    widened = closure - own
    assert len(widened) > 50, (
        "expected restoring 'auth' to cascade far beyond its own tables; "
        f"got {len(widened)} extra"
    )
    # Spot-check tables an operator would be dismayed to lose silently.
    for table in ("appliance", "change_request", "saved_view", "time_bound_grant"):
        if table in Base.metadata.tables:
            assert table in closure


def test_closure_reaches_uncatalogued_tables() -> None:
    """The whole point: CASCADE does not care about the catalog.

    Classifying the unclassified tables — the fix #781 originally
    sketched — would not have helped, because CASCADE reaches them
    whether or not a section claims them.
    """
    catalog = all_section_tables()
    uncatalogued = {t for t in Base.metadata.tables} - catalog
    assert uncatalogued, "expected some tables outside the catalog"

    reached = cascade_closure(set(tables_for_sections(["auth"]))) & uncatalogued
    assert reached, (
        "restoring a catalogued section should reach uncatalogued tables — "
        "that is precisely why classification alone was not the fix"
    )
