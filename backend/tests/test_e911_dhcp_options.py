"""DHCP location options 99 / 123 (#972 Phase 2).

Every test here DECODES the bytes back rather than comparing them to a
hex literal I typed. A literal proves the encoder still produces what it
produced when the test was written, which is not the same as producing
what RFC 4776 and RFC 6225 specify — and a byte-order or bit-packing
mistake produces a perfectly plausible literal.

The fail-closed cases carry as much weight as the happy ones. The blast
radius of a malformed option is not a bad location: it is Kea refusing the
whole configuration and DHCP stopping for every client on the server.

HOW TO RUN:
    make test-one T=tests/test_e911_dhcp_options.py
"""

from __future__ import annotations

import uuid

import pytest

from app.models.e911 import CIVIC_ELEMENTS, EmergencyResponseLocation
from app.services.e911.dhcp_options import (
    DATUM_WGS84,
    MAX_OPTION_BYTES,
    WHAT_NETWORK_ELEMENT,
    encode_option_99,
    encode_option_123,
    kea_option_data,
)


def _erl(**kw) -> EmergencyResponseLocation:
    # Unique by default — `uq_erl_name` makes a second row called "x" a
    # conflict, which the batched test creates five of.
    base = dict(
        name=f"erl-{uuid.uuid4().hex[:10]}",
        country="US",
        a1="NY",
        a3="New York",
        rd="Broadway",
        hno="1234",
    )
    base.update(kw)
    return EmergencyResponseLocation(**base)


def _decode_99(payload: bytes) -> tuple[int, str, dict[int, str]]:
    """Walk an RFC 4776 payload back apart: what, country, {CAtype: value}."""
    what = payload[0]
    country = payload[1:3].decode("ascii")
    out: dict[int, str] = {}
    i = 3
    while i < len(payload):
        catype, length = payload[i], payload[i + 1]
        value = payload[i + 2 : i + 2 + length]
        assert len(value) == length, "a TLV runs past the end of the option"
        out[catype] = value.decode("utf-8")
        i += 2 + length
    return what, country, out


def _decode_123(payload: bytes) -> dict[str, object]:
    """Unpack the 128-bit RFC 6225 structure."""
    assert len(payload) == 16
    bits = int.from_bytes(payload, "big")

    def take(width: int, offset: int) -> int:
        return (bits >> offset) & ((1 << width) - 1)

    # Offsets from the least-significant end, in reverse field order.
    datum = take(8, 0)
    altitude = take(30, 8)
    alt_res = take(6, 38)
    at = take(4, 44)
    lon_raw = take(34, 48)
    lo_res = take(6, 82)
    lat_raw = take(34, 88)
    la_res = take(6, 122)

    def signed(v: int, width: int) -> int:
        return v - (1 << width) if v >= (1 << (width - 1)) else v

    return {
        "la_res": la_res,
        "latitude": signed(lat_raw, 34) / (1 << 25),
        "lo_res": lo_res,
        "longitude": signed(lon_raw, 34) / (1 << 25),
        "at": at,
        "alt_res": alt_res,
        "altitude": signed(altitude, 30) / (1 << 8),
        "datum": datum,
    }


# ══════════════════════════════════════════════════════════════════════
# Option 99 — civic
# ══════════════════════════════════════════════════════════════════════


def test_the_payload_decodes_back_to_the_address() -> None:
    erl = _erl(flr="3", room="312", bld="A")
    what, country, tlvs = _decode_99(encode_option_99(erl))
    assert what == WHAT_NETWORK_ELEMENT
    assert country == "US"
    # CAtypes from RFC 4776 §3.4: A1=1, A3=3, HNO=19, BLD=25, FLR=27,
    # ROOM=28, RD=34.
    assert tlvs[1] == "NY"
    assert tlvs[3] == "New York"
    assert tlvs[19] == "1234"
    assert tlvs[25] == "A"
    assert tlvs[27] == "3"
    assert tlvs[28] == "312"
    assert tlvs[34] == "Broadway"


def test_the_catypes_come_from_the_one_shared_table() -> None:
    """The same tuple that defines the columns and the PIDF-LO tags. A second
    hand-maintained CAtype table is how #878's two renderers diverged."""
    erl = _erl(seat="14")
    _what, _country, tlvs = _decode_99(encode_option_99(erl))
    by_column = {c: n for c, n, _t, _d in CIVIC_ELEMENTS if n is not None}
    assert tlvs[by_column["seat"]] == "14"


def test_what_is_network_element_not_client() -> None:
    """RFC 4776 §3.3. A DHCP scope describes where the serving network is,
    not where the handset is; claiming 2 ("client") would assert a precision
    a subnet-level answer cannot have."""
    assert encode_option_99(_erl())[0] == WHAT_NETWORK_ELEMENT


def test_absent_elements_are_simply_absent() -> None:
    erl = _erl(room=None, flr=None)
    _w, _c, tlvs = _decode_99(encode_option_99(erl))
    assert 27 not in tlvs and 28 not in tlvs


def test_an_empty_string_is_not_encoded() -> None:
    """An empty TLV is a claim that the room is the empty string."""
    _w, _c, tlvs = _decode_99(encode_option_99(_erl(room="")))
    assert 28 not in tlvs


@pytest.mark.parametrize("bad", [None, "", "U", "USA", "12", "u$"])
def test_no_usable_country_means_no_option_at_all(bad) -> None:
    """RFC 4776 puts the country in a FIXED two-byte field. Without one the
    option is not incomplete, it is unparseable — so emit nothing rather
    than bytes a consumer will misread."""
    assert encode_option_99(_erl(country=bad)) is None


def test_a_lowercase_country_is_normalised() -> None:
    _w, country, _t = _decode_99(encode_option_99(_erl(country="us")))
    assert country == "US"


def test_country_only_is_not_worth_an_option() -> None:
    """ "This country, no further detail" is not a dispatchable location."""
    bare = EmergencyResponseLocation(name="bare", country="US")
    assert encode_option_99(bare) is None


def test_the_option_never_exceeds_255_bytes() -> None:
    """A DHCP option's length is one byte. Overflowing it is not a long
    option, it is a corrupt packet."""
    erl = _erl(
        loc="x" * 200,
        nam="y" * 200,
        lmk="z" * 200,
        a4="w" * 200,
        a5="v" * 200,
    )
    payload = encode_option_99(erl)
    assert payload is not None
    assert len(payload) <= MAX_OPTION_BYTES


def test_a_truncated_tlv_is_never_emitted() -> None:
    """The decoder asserts each TLV fits, so this also proves the encoder
    stops at an element boundary rather than mid-value — a truncated TLV
    makes every byte after it garbage."""
    erl = _erl(loc="x" * 250, nam="y" * 250)
    payload = encode_option_99(erl)
    assert payload is not None
    _decode_99(payload)  # raises if a TLV runs past the end


def test_an_element_too_long_for_a_tlv_is_dropped_not_truncated() -> None:
    """Truncating UTF-8 mid-sequence yields bytes a consumer cannot decode.
    The rest of the address is still useful."""
    erl = _erl(loc="é" * 200)  # 400 bytes encoded
    payload = encode_option_99(erl)
    assert payload is not None
    _what, _c, tlvs = _decode_99(payload)
    assert 22 not in tlvs  # LOC dropped
    assert tlvs[1] == "NY"  # the rest survived


def test_multibyte_values_carry_a_byte_length_not_a_character_count() -> None:
    erl = _erl(a3="München")
    _w, _c, tlvs = _decode_99(encode_option_99(erl))
    assert tlvs[3] == "München"


# ══════════════════════════════════════════════════════════════════════
# Option 123 — geodetic
# ══════════════════════════════════════════════════════════════════════


def test_no_point_means_no_option() -> None:
    assert encode_option_123(_erl()) is None


def test_half_a_point_means_no_option() -> None:
    erl = _erl()
    erl.latitude = 40.0
    assert encode_option_123(erl) is None


def test_coordinates_round_trip_to_within_the_fixed_point_resolution() -> None:
    erl = _erl()
    erl.latitude = 40.748817
    erl.longitude = -73.985428
    decoded = _decode_123(encode_option_123(erl))
    assert abs(decoded["latitude"] - 40.748817) < 1e-6
    assert abs(decoded["longitude"] - (-73.985428)) < 1e-6
    assert decoded["datum"] == DATUM_WGS84


def test_a_southern_and_western_point_round_trips() -> None:
    """Two's complement. A sign-handling bug puts Sydney in the Atlantic and
    the option still parses."""
    erl = _erl()
    erl.latitude = -33.868820
    erl.longitude = 151.209290
    decoded = _decode_123(encode_option_123(erl))
    assert abs(decoded["latitude"] - (-33.868820)) < 1e-6
    assert abs(decoded["longitude"] - 151.209290) < 1e-6


def test_the_option_is_exactly_sixteen_bytes() -> None:
    erl = _erl()
    erl.latitude = 0
    erl.longitude = 0
    assert len(encode_option_123(erl)) == 16


def test_altitude_in_floors_is_marked_as_floors() -> None:
    """AT=2 means floors, AT=1 metres. A consumer rendering "3" as 3 metres
    above sea level instead of the third floor is the whole point of the
    field."""
    erl = _erl()
    erl.latitude = 40.0
    erl.longitude = -73.0
    erl.altitude = 3
    erl.altitude_unit = "f"
    decoded = _decode_123(encode_option_123(erl))
    assert decoded["at"] == 2
    assert abs(decoded["altitude"] - 3) < 0.01


def test_no_altitude_means_altitude_type_zero() -> None:
    """AT=0 is "unknown". Emitting metres with a zero value would claim sea
    level."""
    erl = _erl()
    erl.latitude = 40.0
    erl.longitude = -73.0
    decoded = _decode_123(encode_option_123(erl))
    assert decoded["at"] == 0
    assert decoded["alt_res"] == 0


def test_an_out_of_range_coordinate_yields_no_option() -> None:
    """Fails closed. A wrapped fixed-point value is a valid-looking location
    somewhere else entirely."""
    erl = _erl()
    erl.latitude = 400
    erl.longitude = 0
    assert encode_option_123(erl) is None


# ══════════════════════════════════════════════════════════════════════
# What reaches Kea
# ══════════════════════════════════════════════════════════════════════


def test_kea_entries_are_hex_with_csv_format_off() -> None:
    """The civic option is a TLV stream and has no CSV spelling; telling Kea
    otherwise makes it reject the configuration."""
    erl = _erl(room="312")
    erl.latitude = 40.0
    erl.longitude = -73.0
    entries = {d["code"]: d for d in kea_option_data(erl)}
    assert set(entries) == {99, 123}
    for d in entries.values():
        assert d["csv-format"] is False
        assert isinstance(d["data"], str)
        bytes.fromhex(str(d["data"]))  # raises if it is not hex


def test_option_99_uses_keas_own_name_and_123_uses_ours() -> None:
    """Measured against kea-dhcp4 3.0.3, and the first attempt produced a
    config Kea REFUSED OUTRIGHT — which stops DHCP for every client, not just
    this option.

    Kea ships a standard definition for 99 and rejects any override of it, so
    99 must go out under ``geoconf-civic``. 123 has no standard definition at
    all (``geoconf`` / ``geolocation`` / ``geoloc`` were all rejected as
    unknown), so it rides on ours.
    """
    erl = _erl(room="312")
    erl.latitude = 40.0
    erl.longitude = -73.0
    by_code = {d["code"]: d for d in kea_option_data(erl)}
    assert by_code[99]["name"] == "geoconf-civic"
    assert by_code[123]["name"] == "spatium-opt-123"


def test_no_option_def_is_shipped_for_code_99() -> None:
    """The bug this nearly shipped. An option-def for 99 fails the ENTIRE
    Kea configuration with "unable to override definition of option '99' in
    standard option space 'dhcp4'"."""
    from app.drivers.dhcp.kea import _KEA_VENDOR_OPTION_DEFS

    assert 99 not in _KEA_VENDOR_OPTION_DEFS
    assert _KEA_VENDOR_OPTION_DEFS[123]["type"] == "binary"


def test_every_entry_carries_a_name_so_the_strip_guard_keeps_it() -> None:
    """``_strip_undefined_code_options`` drops a bare ``{"code": NN}`` entry
    with no matching definition, because emitting an undefined code fails the
    whole config. An unnamed entry here would be silently removed and the
    feature would do nothing."""
    erl = _erl(room="312")
    erl.latitude = 40.0
    erl.longitude = -73.0
    for d in kea_option_data(erl):
        assert d.get("name")
        assert d.get("space") == "dhcp4"


def test_nothing_is_emitted_for_an_unusable_erl() -> None:
    """An ERL that cannot produce a valid option contributes no option-data
    at all, so the rendered Kea config is byte-identical to one without it."""
    assert kea_option_data(EmergencyResponseLocation(name="empty")) == []


# ══════════════════════════════════════════════════════════════════════
# End to end: does it reach a rendered Kea config?
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_bound_subnet_renders_the_location_options(db_session) -> None:
    """The integration. Encoders that work and a bundle that never carries
    them is the #899 class — code that is right in a file nothing renders
    from."""
    from app.services.dhcp.config_bundle import _with_location_options
    from app.services.e911 import effective_subnet_erls

    subnet, erl = await _bound_subnet(db_session)
    erls = await effective_subnet_erls(db_session, [subnet])
    options = _with_location_options(erls, subnet, {}, address_family="ipv4")
    entries = {d["code"]: d for d in options["option_data"]}
    assert set(entries) == {99, 123}
    assert entries[99]["name"] == "geoconf-civic"

    # And the option-def for 123 is SHIPPED. Without it Kea rejects the whole
    # config — "definition for the option 'dhcp4.spatium-opt-123' does not
    # exist" — and DHCP stops for every client on the server. This was a real
    # hole: option_defs_for_option_maps only scanned `code:NN` KEYS, not the
    # raw `option_data` passthrough these ride on, so the def went unshipped
    # until this assertion caught it.
    from app.drivers.dhcp.kea import option_defs_for_option_maps

    defs = {d["code"] for d in option_defs_for_option_maps([options])}
    assert 123 in defs, "the option-def for 123 is not shipped; Kea would refuse the config"
    # ...and 99 must NOT be, because Kea refuses to have its standard
    # definition overridden.
    assert 99 not in defs


@pytest.mark.asyncio
async def test_an_unbound_subnet_changes_nothing(db_session) -> None:
    """Byte-identical to a config without the feature, so an unchanged bundle
    never triggers a spurious Kea reload."""
    from app.services.dhcp.config_bundle import _with_location_options
    from app.services.e911 import effective_subnet_erls

    subnet, _erl = await _bound_subnet(db_session, bind=False)
    erls = await effective_subnet_erls(db_session, [subnet])
    assert _with_location_options(erls, subnet, {}, address_family="ipv4") == {}


@pytest.mark.asyncio
async def test_existing_option_data_is_not_displaced(db_session) -> None:
    """Phone profiles and the importers use the same raw passthrough. This is
    additive, never a replacement."""
    from app.services.dhcp.config_bundle import _with_location_options
    from app.services.e911 import effective_subnet_erls

    subnet, _erl = await _bound_subnet(db_session)
    erls = await effective_subnet_erls(db_session, [subnet])
    mine = {"code": 150, "data": "10.0.0.1"}
    out = _with_location_options(erls, subnet, {"option_data": [mine]}, address_family="ipv4")
    assert mine in out["option_data"]
    assert len(out["option_data"]) == 3


@pytest.mark.asyncio
async def test_a_device_level_rule_does_not_reach_a_dhcp_scope(db_session) -> None:
    """A `switch_port` or `mac` rule identifies a DEVICE, and a DHCP option is
    written once per scope for every client in it — honouring one would hand
    every phone on the floor the location of a single desk."""
    from app.models.e911 import ERLBinding
    from app.services.dhcp.config_bundle import _with_location_options
    from app.services.e911 import effective_subnet_erls

    subnet, erl = await _bound_subnet(db_session, bind=False)
    db_session.add(ERLBinding(erl_id=erl.id, rule_kind="mac", mac_address="aa:bb:cc:dd:ee:09"))
    await db_session.flush()
    erls = await effective_subnet_erls(db_session, [subnet])
    assert _with_location_options(erls, subnet, {}, address_family="ipv4") == {}


async def _bound_subnet(db, *, bind: bool = True):
    import uuid as _u

    from app.models.e911 import ERLBinding
    from app.models.ipam import IPBlock, IPSpace, Subnet

    space = IPSpace(name=f"e911dhcp-{_u.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.66.0.0/16", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=f"10.66.{_u.uuid4().int % 250}.0/24",
        name=f"voice-{_u.uuid4().hex[:6]}",
        subnet_role="voice",
    )
    db.add(subnet)
    erl = _erl(room="312")
    erl.latitude = 40.0
    erl.longitude = -73.0
    db.add(erl)
    await db.flush()
    if bind:
        db.add(ERLBinding(erl_id=erl.id, rule_kind="subnet", subnet_id=subnet.id))
        await db.flush()
    return subnet, erl


# ══════════════════════════════════════════════════════════════════════
# /code-review round 3
# ══════════════════════════════════════════════════════════════════════


def test_the_encode_order_covers_every_catype() -> None:
    """Two lists describing one thing, pinned to each other. An element
    missing from _ENCODE_ORDER would silently never be encoded."""
    from app.services.e911.dhcp_options import _CATYPE_BY_COLUMN, _ENCODE_PAIRS

    assert {c for c, _n in _ENCODE_PAIRS} == set(_CATYPE_BY_COLUMN)


def test_an_overflow_drops_free_text_not_the_room() -> None:
    """THE finding. The encoder stops at an element boundary when it runs out
    of 255 bytes, so whatever is encoded LAST is what gets dropped — and in
    column order that was building / floor / unit / room / seat, because
    `lmk` / `loc` / `nam` are declared ahead of them and are exactly the
    fields an operator writes a sentence into.

    A long "additional location information" therefore silently produced a
    street address with no room, which is the one thing RAY BAUM'S §506 is
    about.
    """
    erl = _erl(
        loc="x" * 200,
        nam="y" * 200,
        lmk="z" * 200,
        bld="A",
        flr="3",
        room="312",
        seat="14",
        unit="2",
    )
    payload = encode_option_99(erl)
    assert payload is not None
    _what, _country, tlvs = _decode_99(payload)
    by_column = {c: n for c, n, _t, _d in CIVIC_ELEMENTS if n is not None}
    # The dispatchable detail survived...
    for column, expected in (
        ("bld", "A"),
        ("flr", "3"),
        ("room", "312"),
        ("unit", "2"),
        ("seat", "14"),
    ):
        assert tlvs.get(by_column[column]) == expected, column
    # ...and the street address too, since a dispatcher needs it to find the
    # building at all.
    assert tlvs[by_column["hno"]] == "1234"
    # Something had to go, and it was the free text.
    assert by_column["loc"] not in tlvs or by_column["nam"] not in tlvs


@pytest.mark.asyncio
async def test_an_ipv6_scope_gets_no_v4_location_options(db_session) -> None:
    """The worst finding of the round. Options 99 and 123 are DHCPv4 codes in
    the `dhcp4` space; the agent's v6 renderer passes the raw passthrough
    through verbatim and kea-dhcp6 then rejects the WHOLE config — the "DHCP
    stops for every client" outcome this feature otherwise avoids.

    An ERL reached by a `vlan` or `site_default` binding applies to both
    families, so this is a real path rather than a hypothetical one.
    """
    from app.services.dhcp.config_bundle import _with_location_options
    from app.services.e911 import effective_subnet_erls

    subnet, _erl_row = await _bound_subnet(db_session)
    erls = await effective_subnet_erls(db_session, [subnet])
    assert erls, "fixture did not bind an ERL"

    v4 = _with_location_options(erls, subnet, {}, address_family="ipv4")
    assert "option_data" in v4

    v6 = _with_location_options(erls, subnet, {}, address_family="ipv6")
    assert v6 == {}, "v4 location options leaked into a Dhcp6 scope"


@pytest.mark.asyncio
async def test_the_subnet_lookup_is_batched(db_session) -> None:
    """Runs inside the agent /config long-poll: three queries per scope is 600
    round trips on a 200-scope server with no ERL bindings at all."""
    from app.services.e911 import effective_subnet_erls

    subnets = []
    for _ in range(5):
        subnet, _e = await _bound_subnet(db_session)
        subnets.append(subnet)
    out = await effective_subnet_erls(db_session, subnets)
    assert len(out) == 5
    # And an empty input costs nothing at all.
    assert await effective_subnet_erls(db_session, []) == {}


def test_the_control_plane_renderer_passes_option_data_through() -> None:
    """The preview tab stringified the whole list into one bogus entry and
    omitted the real ones, so an operator was shown a config that would not
    load. The agent renderer has always had this passthrough."""
    from app.drivers.dhcp.kea import _render_option_data

    entry = {"name": "geoconf-civic", "code": 99, "csv-format": False, "data": "01"}
    out = _render_option_data({"option_data": [entry], "routers": "10.0.0.1"})
    assert entry in out
    assert not any(d.get("name") == "option_data" for d in out)
