"""DHCP location options 99 / 123 — RFC 4776 + RFC 6225 (#972 Phase 2).

A phone that supports these learns its own civic address at lease time,
with no HELD exchange and no LLDP-MED. Few handsets consume them, so this
is a bonus rather than the headline — but it is nearly free, because we
already run the DHCP server and already hold the address as separate
elements.

**Per-scope, never per-client.** DHCP can know which subnet a request came
from and nothing finer, so this is the FLOOR-level answer. A room-level
location cannot travel this way and the docs say so; anyone wanting the
room uses HELD or LLDP-MED.

**Everything here fails closed to "emit no option".** That is the one
property that matters, because the blast radius of getting it wrong is not
a bad location — it is Kea refusing the whole configuration and DHCP
stopping for every client on the server. So: a value that will not fit in
an option is dropped rather than truncated, an element that will not encode
is skipped, a total over 255 bytes yields nothing at all, and every
encoder returns ``None`` rather than a partial result. The agent's #882
config-test-before-apply is a second net, not the first one.

**Option 99 `what` is 1, "network element".** RFC 4776 §3.3 offers 0 (the
DHCP server's own location), 1 (the network element believed closest to the
client) and 2 (the client). 1 is the honest value for a subnet-level
answer: we are describing where the serving network is, not where the
handset is, and claiming 2 would assert a precision a DHCP scope cannot
have.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.e911 import CIVIC_ELEMENTS, EmergencyResponseLocation

#: A DHCP option's value field is a single byte of length, so 255 bytes.
MAX_OPTION_BYTES = 255

#: RFC 4776 §3.3 location-of-what.
WHAT_DHCP_SERVER = 0
WHAT_NETWORK_ELEMENT = 1
WHAT_CLIENT = 2

#: RFC 6225 §2.3: 1 is WGS 84.
DATUM_WGS84 = 1

#: Resolution, in bits of the fixed-point value that are meaningful. 34 is
#: "all of them"; an operator-typed decimal degree pair is good to about 6
#: decimal places, which is ~21 fractional bits, so 9 integer + 21 = 30.
#: Claiming 34 would tell a consumer the value is exact to the millimetre.
LATLON_RESOLUTION_BITS = 30

#: Altitude in metres, with 22 integer bits. An operator typing a floor
#: height is not claiming centimetres; 16 is metre-ish precision.
ALTITUDE_RESOLUTION_BITS = 16

_CATYPE_BY_COLUMN: dict[str, int] = {
    column: catype for column, catype, _tag, _desc in CIVIC_ELEMENTS if catype is not None
}


def encode_option_99(
    erl: EmergencyResponseLocation, *, what: int = WHAT_NETWORK_ELEMENT
) -> bytes | None:
    """RFC 4776 GEOCONF_CIVIC payload, or None when there is nothing to say.

    Layout: ``what`` (1 byte), country code (2 bytes ASCII), then
    ``CAtype, length, value`` triples. ``country`` is its own fixed field
    rather than a CAtype, which is why it is absent from
    ``_CATYPE_BY_COLUMN``.
    """
    country = (erl.country or "").strip().upper()
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        # RFC 4776 requires a two-letter country. Without one the option is
        # not merely incomplete, it is unparseable — so emit nothing.
        return None

    out = bytearray([what & 0xFF])
    out += country.encode("ascii")

    for column, catype in _CATYPE_BY_COLUMN.items():
        raw = getattr(erl, column, None)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        value = text.encode("utf-8")
        if len(value) > 255:
            # One over-long element is dropped; the rest of the address is
            # still useful, and truncating a UTF-8 string mid-sequence would
            # produce bytes a consumer cannot decode.
            continue
        if len(out) + 2 + len(value) > MAX_OPTION_BYTES:
            # Out of room. Stop cleanly rather than emitting a truncated
            # TLV, which would make every byte after it garbage.
            break
        out += bytes([catype, len(value)])
        out += value

    # `what` + country alone says "this country, no further detail", which
    # is not a dispatchable location and not worth a DHCP option.
    if len(out) <= 3:
        return None
    return bytes(out)


def _fixed_point(value: Decimal, int_bits: int, frac_bits: int) -> int | None:
    """Two's-complement fixed point in ``int_bits + frac_bits`` bits."""
    total = int_bits + frac_bits
    scaled = int((value * (1 << frac_bits)).to_integral_value())
    limit = 1 << (total - 1)
    if scaled >= limit or scaled < -limit:
        return None
    return scaled & ((1 << total) - 1)


def encode_option_123(erl: EmergencyResponseLocation) -> bytes | None:
    """RFC 6225 GeoConf payload — 16 bytes — or None without a point.

    Bit layout, most significant first: LaRes(6) Latitude(34) LoRes(6)
    Longitude(34) AT(4) AltRes(6) Altitude(30) Datum(8) = 128 bits.
    """
    if erl.latitude is None or erl.longitude is None:
        return None

    lat = _fixed_point(Decimal(erl.latitude), 9, 25)
    lon = _fixed_point(Decimal(erl.longitude), 9, 25)
    if lat is None or lon is None:
        return None

    # Altitude type: 1 = metres, 2 = floors. An ERL records which.
    if erl.altitude is None:
        at, alt_res, alt = 0, 0, 0
    else:
        at = 2 if (erl.altitude_unit or "m") == "f" else 1
        alt_res = ALTITUDE_RESOLUTION_BITS
        encoded = _fixed_point(Decimal(erl.altitude), 22, 8)
        if encoded is None:
            at, alt_res, alt = 0, 0, 0
        else:
            alt = encoded

    bits = 0
    for value, width in (
        (LATLON_RESOLUTION_BITS, 6),
        (lat, 34),
        (LATLON_RESOLUTION_BITS, 6),
        (lon, 34),
        (at, 4),
        (alt_res, 6),
        (alt, 30),
        (DATUM_WGS84, 8),
    ):
        bits = (bits << width) | (value & ((1 << width) - 1))
    return bits.to_bytes(16, "big")


def kea_option_data(erl: EmergencyResponseLocation) -> list[dict[str, object]]:
    """Kea ``option-data`` entries for this ERL's location options.

    **The names here were measured, not assumed** — against kea-dhcp4 3.0.3,
    and the first attempt produced a configuration Kea REFUSED OUTRIGHT,
    which would have stopped DHCP for every client on the server:

    * Option 99 has a standard Kea definition and an ``option-def`` for it is
      rejected with "unable to override definition of option '99' in standard
      option space 'dhcp4'". So it is emitted under Kea's own name,
      ``geoconf-civic``.
    * Option 123 has **no** standard definition — ``geoconf``,
      ``geolocation`` and ``geoloc`` were all rejected as unknown — so it
      rides on our own ``spatium-opt-123`` def in
      ``drivers/dhcp/kea._KEA_VENDOR_OPTION_DEFS``.

    Both entries carry a ``name``, which is also what keeps them past
    ``_strip_undefined_code_options``: that guard drops a bare
    ``{"code": NN}`` entry with no matching definition, because emitting an
    undefined code fails the whole config.

    ``csv-format: false`` on both: the civic option is a TLV stream and the
    geodetic one a packed bitfield, and neither has a CSV spelling. Kea
    happens to accept the hex without it, but that depends on what its
    standard definition is — being explicit does not.
    """
    out: list[dict[str, object]] = []
    civic = encode_option_99(erl)
    if civic is not None:
        out.append(
            {
                "name": "geoconf-civic",
                "code": 99,
                "space": "dhcp4",
                "csv-format": False,
                "data": civic.hex().upper(),
            }
        )
    geo = encode_option_123(erl)
    if geo is not None:
        out.append(
            {
                "name": "spatium-opt-123",
                "code": 123,
                "space": "dhcp4",
                "csv-format": False,
                "data": geo.hex().upper(),
            }
        )
    return out
