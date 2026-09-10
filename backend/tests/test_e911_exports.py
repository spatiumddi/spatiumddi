"""E911 exports — CSV and IOS LLDP-MED snippets (#972 Phase 3).

The formatting is the easy half. These tests are mostly about **injection**,
because both outputs interpolate operator-typed free text into a target that
treats a newline as a statement separator:

* a room named ``312\\nno logging console`` in an IOS snippet is arbitrary
  configuration pasted into a switch by an operator who trusted us;
* a building named ``=cmd|' /C calc'!A0`` in a CSV is a formula that executes
  when the file is opened in Excel.

And the *reporting* matters as much as the refusal: a value we cannot express
is omitted WITH a comment, because an ERL visibly missing its room is a
problem an operator fixes, where a silently-truncated one is a phone in the
wrong place that looks correct.

HOW TO RUN:
    make test-one T=tests/test_e911_exports.py
"""

from __future__ import annotations

import csv
import io
import re
import uuid

import pytest

from app.models.e911 import CIVIC_ELEMENTS, EmergencyResponseLocation, ERLBinding
from app.services.e911.exports import (
    CSV_COLUMNS,
    IOS_CIVIC_KEYWORDS,
    render_csv,
    render_ios_snippets,
)


def _erl(**kw) -> EmergencyResponseLocation:
    base = dict(
        name="Bldg A — Floor 3 — Room 312",
        country="US",
        a1="NY",
        a3="New York",
        rd="Broadway",
        hno="1234",
        bld="A",
        flr="3",
        room="312",
    )
    base.update(kw)
    erl = EmergencyResponseLocation(**base)
    erl.id = uuid.uuid4()
    return erl


def _live_lines(snippet: str) -> list[str]:
    """The lines an operator would actually paste into a switch.

    Everything starting with ``!`` is an IOS comment and is inert, which is
    where omitted values are reported. Asserting "the token is absent from the
    whole output" would be wrong — it is *supposed* to appear in the OMITTED
    list — and naive ``.replace()`` of the comment prefix left the value
    behind, which is how the first version of these tests failed for the right
    reason.
    """
    return [ln for ln in snippet.splitlines() if not ln.lstrip().startswith("!")]


def _csv_rows(rows) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(render_csv(rows))))


# ══════════════════════════════════════════════════════════════════════
# CSV
# ══════════════════════════════════════════════════════════════════════


def test_every_civic_element_has_a_column() -> None:
    """An element with no column is data the export silently loses."""
    for column, _n, _t, _d in CIVIC_ELEMENTS:
        assert column in CSV_COLUMNS, column


def test_the_address_round_trips() -> None:
    row = _csv_rows([(_erl(), [])])[0]
    assert row["erl_name"].startswith("Bldg A")
    assert row["country"] == "US"
    assert row["room"] == "312"
    assert row["is_dispatchable"] == "yes"


def test_a_street_only_erl_is_marked_not_dispatchable() -> None:
    """The RAY BAUM'S gap, visible in the spreadsheet an auditor asked for."""
    row = _csv_rows([(_erl(bld=None, flr=None, room=None), [])])[0]
    assert row["is_dispatchable"] == "no"


def test_bindings_are_summarised() -> None:
    erl = _erl()
    bindings = [
        ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=uuid.uuid4()),
        ERLBinding(erl_id=erl.id, rule_kind="site_default", site_id=uuid.uuid4()),
    ]
    row = _csv_rows([(erl, bindings)])[0]
    assert row["binding_count"] == "2"
    assert set(row["binding_rules"].split()) == {"subnet", "site_default"}


@pytest.mark.parametrize(
    "hostile",
    [
        "=cmd|' /C calc'!A0",
        "+1+1",
        "-2+3",
        "@SUM(A1:A9)",
    ],
)
def test_a_formula_is_neutralised(hostile: str) -> None:
    """Prefixed with a single quote, which every spreadsheet reads as "this is
    text". Stripping the character instead would alter an address that
    legitimately starts with a hyphen."""
    row = _csv_rows([(_erl(bld=hostile), [])])[0]
    assert row["bld"] == "'" + hostile


def test_an_embedded_newline_stays_inside_its_field() -> None:
    """CSV quoting handles this properly, unlike the IOS target — so the value
    is preserved rather than dropped. What must not happen is the row
    splitting in two."""
    rows = _csv_rows([(_erl(room="312\nannex"), [])])
    assert len(rows) == 1
    assert rows[0]["room"] == "312\nannex"


def test_the_file_uses_crlf_line_endings() -> None:
    """RFC 4180, and what every spreadsheet on Windows expects."""
    out = render_csv([(_erl(), [])])
    assert "\r\n" in out
    assert out.count("\r\n") >= 2  # header + one row


def test_an_empty_export_still_has_a_header() -> None:
    """A zero-row CSV with no header is indistinguishable from a failed
    download."""
    out = render_csv([])
    assert out.splitlines()[0].startswith("erl_name")


# ══════════════════════════════════════════════════════════════════════
# IOS LLDP-MED snippets
# ══════════════════════════════════════════════════════════════════════


def test_the_snippet_says_it_is_generated_for_review() -> None:
    """SpatiumDDI configures no switches. An operator pasting this must know
    nobody has applied it to a device."""
    out = render_ios_snippets([(_erl(), [])])
    assert "GENERATED FOR REVIEW" in out
    assert "does not configure switches" in out


def test_the_civic_stanza_carries_the_address() -> None:
    out = render_ios_snippets([(_erl(), [])])
    assert re.search(r"location civic-location identifier spatium-[0-9a-f]{8}", out)
    assert " country US" in out
    assert " state NY" in out
    assert " city New York" in out
    assert " number 1234" in out
    assert " floor 3" in out
    assert " room 312" in out
    assert " exit" in out


def test_a_newline_injection_is_omitted_and_reported() -> None:
    """THE test. A room named "312\\nno logging console" would otherwise be
    arbitrary configuration pasted into a switch — and truncating it to "312"
    silently would be a phone in the wrong place that looks correct."""
    hostile = "312\nno logging console\n!"
    out = render_ios_snippets([(_erl(room=hostile), [])])
    # It may appear in the OMITTED comment — it must never appear on a line an
    # operator would paste.
    assert not any("no logging" in ln for ln in _live_lines(out))
    assert "OMITTED" in out
    assert "room=" in out


@pytest.mark.parametrize("hostile", ["a\rb", "a\nb", "a\x00b", "a\x1bb", "x" * 300])
def test_values_that_cannot_be_expressed_safely_are_dropped(hostile: str) -> None:
    out = render_ios_snippets([(_erl(room=hostile), [])])
    assert not any(ln.strip().startswith("room ") for ln in _live_lines(out))
    assert "OMITTED" in out


def test_an_elin_that_is_not_a_number_is_refused() -> None:
    """`location elin-location` takes a dialable number. Anything else is a
    command argument nobody checked."""
    erl = _erl()
    erl.elins = ["+12125550199", "'; reload"]
    out = render_ios_snippets([(erl, [])])
    assert re.search(r"location elin-location \+12125550199 identifier spatium-[0-9a-f]{8}", out)
    assert not any("reload" in ln for ln in _live_lines(out))


def test_elements_with_no_ios_keyword_are_reported_not_dropped_silently() -> None:
    """The phone will not announce `seat`, and the operator should know that
    rather than assume it was sent."""
    out = render_ios_snippets([(_erl(seat="14"), [])])
    assert "NOT SENT" in out
    assert "seat" in out
    assert not any("seat" in ln for ln in _live_lines(out))


def test_the_identifier_is_ios_safe_not_the_erl_name() -> None:
    """IOS identifiers are short and may not contain spaces; an ERL name is
    neither. The real name goes in a comment so an operator can tell which
    stanza is which."""
    erl = _erl(name="Bldg A — Floor 3 — Room 312")
    out = render_ios_snippets([(erl, [])])
    assert re.search(r"identifier spatium-[0-9a-f]{8}", out)
    assert "! Bldg A — Floor 3 — Room 312" in out


def test_per_interface_lines_are_emitted_for_a_switch_port_binding() -> None:
    erl = _erl()
    out = render_ios_snippets(
        [(erl, [])],
        interface_names={str(erl.id): [("sw-fl3-a", "GigabitEthernet3/0/12")]},
    )
    assert "interface GigabitEthernet3/0/12" in out
    assert re.search(r" location civic-location-id spatium-[0-9a-f]{8}", out)


def test_a_hostile_interface_name_is_refused() -> None:
    erl = _erl()
    out = render_ios_snippets(
        [(erl, [])],
        interface_names={str(erl.id): [("sw-a", "Gi1/0/1\n no shutdown")]},
    )
    assert not any("no shutdown" in ln for ln in _live_lines(out))
    assert "OMITTED" in out


def test_each_erl_gets_its_own_identifier() -> None:
    a, b = _erl(name="A"), _erl(name="B", room="401")
    out = render_ios_snippets([(a, []), (b, [])])
    idents = set(re.findall(r"identifier (spatium-[0-9a-f]{8})", out))
    assert len(idents) == 2, "two ERLs must not share an identifier"


def test_an_empty_export_says_so() -> None:
    out = render_ios_snippets([])
    assert "No Emergency Response Locations" in out


def test_every_ios_keyword_maps_a_real_column() -> None:
    """A keyword for a column that does not exist is a stanza line that never
    renders — the #899 "written, never read" class."""
    columns = {c for c, _n, _t, _d in CIVIC_ELEMENTS}
    assert set(IOS_CIVIC_KEYWORDS) <= columns


# ══════════════════════════════════════════════════════════════════════
# /code-review round 3
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("coord", ["-73.985428", "-33.86882", "-0.5", "-120"])
def test_a_negative_coordinate_is_not_quoted_as_text(coord: str) -> None:
    """`-` is a formula leader and every Southern or Western coordinate starts
    with one. Quoting those imported latitude, longitude and altitude as TEXT
    in the spreadsheet this export exists for, so the columns could not be
    sorted, plotted or summed."""
    erl = _erl()
    erl.longitude = coord
    row = _csv_rows([(erl, [])])[0]
    assert row["longitude"] == coord


def test_a_leading_hyphen_that_is_not_a_number_is_still_quoted() -> None:
    """The narrowing is "is it a number", not "does it start with a hyphen"."""
    row = _csv_rows([(_erl(bld="-cmd|calc"), [])])[0]
    assert row["bld"] == "'-cmd|calc"


def test_the_identifier_is_derived_from_the_erl_id_not_its_position() -> None:
    """The first version was positional and documented as "stable" while being
    the opposite: adding one ERL renumbered every later stanza, so an operator
    who had already applied an export would re-point live port assignments at
    the wrong locations the next time they pasted."""
    a, b = _erl(name="A"), _erl(name="B")
    first = render_ios_snippets([(a, [])])
    # Insert another ERL ahead of it — a's identifier must not move.
    second = render_ios_snippets([(b, []), (a, [])])
    ident_a = [ln for ln in first.splitlines() if "identifier spatium-" in ln][0]
    assert ident_a in second, "a's identifier changed when another ERL was inserted"


def test_per_interface_lines_name_their_switch() -> None:
    """`Gi1/0/12` exists on every switch in the estate. A file of bare
    interface names gives an operator no way to tell which lines belong to the
    switch in front of them, and pasting the wrong ones mis-assigns ports to
    rooms."""
    erl = _erl()
    out = render_ios_snippets(
        [(erl, [])],
        interface_names={str(erl.id): [("sw-fl3-a", "GigabitEthernet3/0/12")]},
    )
    assert "! on sw-fl3-a" in out
    assert "interface GigabitEthernet3/0/12" in out
    # The heading comes before the interface it describes.
    lines = out.splitlines()
    assert lines.index("! on sw-fl3-a") < lines.index("interface GigabitEthernet3/0/12")


def test_two_switches_sharing_a_port_name_are_distinguishable() -> None:
    a, b = _erl(name="Room 312"), _erl(name="Room 401")
    out = render_ios_snippets(
        [(a, []), (b, [])],
        interface_names={
            str(a.id): [("sw-fl3-a", "Gi1/0/12")],
            str(b.id): [("sw-fl4-a", "Gi1/0/12")],
        },
    )
    assert "! on sw-fl3-a" in out and "! on sw-fl4-a" in out
