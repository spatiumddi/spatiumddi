"""The seeder's 409 zone lookup survives the API's name canonicalisation (#981).

The bug this pins: ``_get_or_create_zone`` matched the API's zone list
against the string it POSTed, and the API stores the canonical form
(lower-cased, root dot appended by ``ZoneCreate.validate_zone_name``).
The comparison was therefore false for every zone that existed, so the
409 fallback could never fire and any re-seed of a manifest naming a
fixed zone died in the seeder before a single DORA.

``find_existing_zone`` is tested rather than ``_get_or_create_zone``
itself because the latter imports ``httpx``, which the hermetic
``make perf-test`` runner does not have. What that leaves untested here
is the HTTP plumbing around the helper; what it does cover is the
comparison, which is where the defect was, and
``test_second_post_resolves_to_the_first_zones_id`` walks the whole
POST → 409 → list → match round trip against a fake API that
canonicalises the way the real one does.

``is_reverse_zone`` is here for the same reason and the same bug one
layer over: ``api_mutation_stream`` and ``synthetic_ui_probe`` selected a
target zone with ``not name.endswith(".arpa")``, false for every stored
reverse zone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spddi_perf.zone_names import (  # noqa: E402
    find_existing_zone,
    is_reverse_zone,
    normalize_zone_name,
)


def zone(name: str, zid: str = "z1", view_id: str | None = None) -> dict:
    """One row shaped like ``GET /v1/dns/groups/{gid}/zones`` returns."""
    return {"id": zid, "name": name, "view_id": view_id}


# ── normalize_zone_name ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("burst.ddipg.test", "burst.ddipg.test"),
        ("burst.ddipg.test.", "burst.ddipg.test"),
        # The half a bare rstrip('.') would miss: validate_fqdn lower-cases
        # every label, so a manifest with a capital is the same bug.
        ("Burst.DDIPG.test", "burst.ddipg.test"),
        ("BURST.DDIPG.TEST.", "burst.ddipg.test"),
        ("107.10.in-addr.arpa.", "107.10.in-addr.arpa"),
        ("  spaced.test.  ", "spaced.test"),
    ],
)
def test_normalize(raw: str, want: str) -> None:
    assert normalize_zone_name(raw) == want


def test_normalize_is_idempotent() -> None:
    once = normalize_zone_name("Burst.DDIPG.test.")
    assert normalize_zone_name(once) == once


# ── find_existing_zone ──────────────────────────────────────────────────


def test_finds_the_zone_the_api_reports_with_a_trailing_dot() -> None:
    """The reported failure: seeder POSTed undotted, API lists dotted."""
    zones = [zone("burst.ddipg.test.", "fwd-1"), zone("other.test.", "z9")]
    found = find_existing_zone(zones, "burst.ddipg.test")
    assert found is not None and found["id"] == "fwd-1"


def test_finds_a_reverse_zone() -> None:
    """`107.10.in-addr.arpa` — the zone named in #981's traceback."""
    zones = [zone("107.10.in-addr.arpa.", "rev-107")]
    found = find_existing_zone(zones, "107.10.in-addr.arpa")
    assert found is not None and found["id"] == "rev-107"


def test_finds_a_zone_the_manifest_spelled_with_capitals() -> None:
    zones = [zone("campus.ddipg.test.", "fwd-2")]
    found = find_existing_zone(zones, "Campus.DDIPG.test")
    assert found is not None and found["id"] == "fwd-2"


def test_absent_zone_is_not_matched() -> None:
    zones = [zone("other.test.", "z9"), zone("10.in-addr.arpa.", "z8")]
    assert find_existing_zone(zones, "burst.ddipg.test") is None


def test_empty_group_is_not_matched() -> None:
    assert find_existing_zone([], "burst.ddipg.test") is None


def test_a_same_named_zone_in_a_view_is_not_the_conflict() -> None:
    """Uniqueness is (group, view, name) and the seeder POSTs no view.

    Returning the viewed zone would load the run's records behind that
    view's match-clients, where the query generator would never see them
    — a silently wrong run rather than a loud failure.
    """
    zones = [zone("burst.ddipg.test.", "viewed", view_id="v-1")]
    assert find_existing_zone(zones, "burst.ddipg.test") is None


def test_the_unviewed_zone_wins_when_both_exist() -> None:
    zones = [
        zone("burst.ddipg.test.", "viewed", view_id="v-1"),
        zone("burst.ddipg.test.", "unviewed"),
    ]
    found = find_existing_zone(zones, "burst.ddipg.test")
    assert found is not None and found["id"] == "unviewed"


def test_a_row_without_a_view_id_key_reads_as_unviewed() -> None:
    """Degrade toward matching if a server ever omits the field."""
    found = find_existing_zone([{"id": "fwd-3", "name": "burst.ddipg.test."}], "burst.ddipg.test")
    assert found is not None and found["id"] == "fwd-3"


# ── The round trip #981 asks for ────────────────────────────────────────


class FakeZoneApi:
    """Enough of the zones API to reproduce the reported failure.

    Canonicalises exactly as the backend does — ``validate_fqdn``
    lower-cases each label and ``ZoneCreate.validate_zone_name`` appends
    the root dot — and 409s on a duplicate ``(view_id, canonical name)``,
    which is the constraint ``create_zone`` checks.
    """

    def __init__(self) -> None:
        self.zones: list[dict] = []
        self._n = 0

    @staticmethod
    def _canonical(name: str) -> str:
        return name.strip().rstrip(".").lower() + "."

    def post(self, name: str, view_id: str | None = None) -> dict:
        canonical = self._canonical(name)
        for z in self.zones:
            if z["name"] == canonical and z["view_id"] == view_id:
                raise Conflict409()
        self._n += 1
        row = {"id": f"zone-{self._n}", "name": canonical, "view_id": view_id}
        self.zones.append(row)
        return row

    def list(self) -> list[dict]:
        return list(self.zones)


class Conflict409(Exception):
    pass


def test_second_post_resolves_to_the_first_zones_id() -> None:
    """POST a zone twice; the 409 fallback returns the first zone's id.

    This is the seeder's control flow with the HTTP taken out. Before the
    fix the list comparison was ``z["name"] == zname`` and this test's
    final assertion was a RuntimeError instead.
    """
    api = FakeZoneApi()
    first = api.post("burst.ddipg.test")

    with pytest.raises(Conflict409):
        api.post("burst.ddipg.test")

    found = find_existing_zone(api.list(), "burst.ddipg.test")
    assert found is not None
    assert found["id"] == first["id"]


def test_the_old_comparison_is_what_broke() -> None:
    """Pins the cause, so the fix cannot be reverted as cosmetic.

    A raw equality test against the API's name fails for a zone that is
    plainly there — which is the whole of #981.
    """
    api = FakeZoneApi()
    api.post("burst.ddipg.test")
    assert not any(z["name"] == "burst.ddipg.test" for z in api.list())
    assert find_existing_zone(api.list(), "burst.ddipg.test") is not None


# ── is_reverse_zone ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "0.10.in-addr.arpa.",
        "0.10.in-addr.arpa",
        "107.10.IN-ADDR.ARPA.",
        "8.b.d.0.1.0.0.2.ip6.arpa.",
        "arpa.",
    ],
)
def test_reverse_zones_are_recognised(name: str) -> None:
    assert is_reverse_zone(name)


@pytest.mark.parametrize(
    "name",
    ["campus.example.edu.", "burst.ddipg.test", "arpanet.example.com.", ""],
)
def test_forward_zones_are_not_reverse(name: str) -> None:
    """``arpanet.example.com`` is the reason this is a LABEL suffix test."""
    assert not is_reverse_zone(name)


def test_the_mutation_stream_picks_a_forward_zone() -> None:
    """The selection api_mutation_stream / synthetic_ui_probe make.

    ``list_zones`` orders by name, so the reverse zone sorts first — with
    the old ``endswith(".arpa")`` (false for a stored, dotted name) the
    filter kept it and ``fwd[0]`` was the reverse zone, sending every
    ``perf-op-*`` A record into it for the length of the run.
    """
    listed = [zone("0.10.in-addr.arpa.", "rev"), zone("campus.example.edu.", "fwd")]
    picked = [z for z in listed if not is_reverse_zone(str(z.get("name", "")))]
    assert [z["id"] for z in picked] == ["fwd"]
    # And the expression it replaced would have picked the reverse zone.
    old = [z for z in listed if not str(z.get("name", "")).endswith(".arpa")]
    assert old[0]["id"] == "rev"
