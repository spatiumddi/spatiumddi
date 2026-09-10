"""PIDF-LO rendering — RFC 4119 + RFC 5139 civic + RFC 5491 (#972 Phase 1b).

A Presence Information Data Format Location Object is what an E911
provider, a PBX or a phone actually consumes: the civic address as
separate XML elements inside a ``<geopriv>`` wrapper carrying usage rules
and a ``method`` saying how the location was determined.

**This is the payoff for the 31-column decision.** The element names come
straight out of ``CIVIC_ELEMENTS`` in ``app.models.e911`` — the same tuple
that defines the columns and the RFC 4776 CAtype numbers — so the schema,
this renderer and the Phase 2 DHCP option-99 encoder cannot disagree. A
second hand-maintained list of element names is exactly how #878's two
blocklist renderers came to emit different rdata for the same row.

**The ``method`` element is not decoration.** RFC 4119 registers tokens
for how a location was determined, and a consumer treats them differently:
``Wiremap`` means somebody traced a cable, which is the strongest civic
claim short of a survey, where ``Manual`` means an operator typed it. Our
binding rules map onto those tokens directly, so a provider receiving our
PIDF-LO can tell a port-level answer from a building-level one without
parsing our prose.

**``retransmission-allowed`` is ``no``.** RFC 4119's usage rules are how
the holder of a location states what may be done with it, and this one is
the location of a person at a desk. A 911 path needs no onward
distribution rights, and defaulting to ``yes`` would hand every recipient
permission we have no business granting.

Serialised with ``xml.etree.ElementTree``, which escapes text and
attributes correctly. Namespace prefixes are registered so the output
carries the conventional ``cl:`` / ``gp:`` / ``gml:`` spellings rather than
ElementTree's ``ns0:`` — readable by a human staring at a packet capture,
which is most of what debugging an E911 integration consists of.

**Verification status, stated plainly:** the structure below follows RFC
4119 §5 and RFC 5139 §4, and the tests pin well-formedness, namespaces,
element naming, ordering and escaping. It has **not** been exchanged with
a live CUCM, RedSky or Intrado endpoint, and the docs say so. Treat the
element set as correct and the interop as unproven.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from app.models.e911 import CIVIC_ELEMENTS, EmergencyResponseLocation

NS_PIDF = "urn:ietf:params:xml:ns:pidf"
NS_GEOPRIV = "urn:ietf:params:xml:ns:pidf:geopriv10"
NS_CIVIC = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr"
NS_BASIC_POLICY = "urn:ietf:params:xml:ns:pidf:geopriv10:basicPolicy"
NS_GML = "http://www.opengis.net/gml"

#: WGS 84, the CRS every E911 consumer expects. RFC 5491 §5 requires the
#: URN form rather than the bare "EPSG:4326" spelling.
SRS_NAME = "urn:ogc:def:crs:EPSG::4326"

#: Binding rule → RFC 4119 Geopriv ``method`` token.
#:
#: ``Wiremap`` for a switch port: the location was derived from the
#: physical cable plant, which is the strongest civic claim we can make.
#: ``Manual`` for everything else, because an operator asserted it — a
#: subnet or site rule is a statement about a network, not a measurement
#: of a device. Deliberately NOT ``DHCP-option`` for the subnet rule: that
#: token means the DEVICE learned its location from a DHCP option, which
#: is Phase 2 and is not what happened here.
RULE_METHOD: dict[str, str] = {
    "switch_port": "Wiremap",
    "wireless_ap": "Wiremap",
    "mac": "Manual",
    "ip": "Manual",
    "subnet": "Manual",
    "vlan": "Manual",
    "site_default": "Manual",
}

#: The order civic elements are EMITTED in, which is not the order the
#: columns are declared in.
#:
#: Column order is what suits the schema and the UI form; this is the RFC
#: 5139 §4 schema sequence. They differ — the RFC puts the road group (RD,
#: STS, POD …) immediately after the administrative divisions, where the
#: column list keeps the legacy A6 spelling there and the road group near
#: the end. A consumer that validates the document against the published
#: schema rejects out-of-order children of an ``xs:sequence``, and one that
#: does not will not notice; emitting in schema order costs nothing and is
#: the only option that works for both.
#:
#: **Not verified against the published .xsd** — see the module docstring.
#: If a real client rejects a document, this tuple is the single place to
#: correct, and ``test_pidf_order_covers_every_civic_element`` guarantees it
#: stays exhaustive so a new element cannot silently vanish from the wire.
PIDF_ELEMENT_ORDER: tuple[str, ...] = (
    "country",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "A6",
    "PRM",
    "PRD",
    "RD",
    "STS",
    "POD",
    "POM",
    "RDSEC",
    "RDBR",
    "RDSUBBR",
    "HNO",
    "HNS",
    "LMK",
    "LOC",
    "FLR",
    "NAM",
    "PC",
    "BLD",
    "UNIT",
    "ROOM",
    "SEAT",
    "PLC",
    "PCN",
    "POBOX",
    "ADDCODE",
)

_TAG_BY_COLUMN: dict[str, str] = {column: tag for column, _catype, tag, _desc in CIVIC_ELEMENTS}
_COLUMN_BY_TAG: dict[str, str] = {tag: column for column, tag in _TAG_BY_COLUMN.items()}

#: ``(column, tag)`` in wire order.
_CIVIC_TAGS: tuple[tuple[str, str], ...] = tuple(
    (_COLUMN_BY_TAG[tag], tag) for tag in PIDF_ELEMENT_ORDER if tag in _COLUMN_BY_TAG
)


def _register_prefixes() -> None:
    ET.register_namespace("", NS_PIDF)
    ET.register_namespace("gp", NS_GEOPRIV)
    ET.register_namespace("cl", NS_CIVIC)
    ET.register_namespace("gbp", NS_BASIC_POLICY)
    ET.register_namespace("gml", NS_GML)


def civic_element_pairs(
    erl: EmergencyResponseLocation,
) -> list[tuple[str, str]]:
    """``(PIDF tag, value)`` for every civic element this ERL actually sets.

    Absent elements are OMITTED rather than emitted empty: an empty
    ``<cl:ROOM/>`` is a claim that the room is the empty string, and a
    consumer reasonably renders it as a room.

    Emitted in ``PIDF_ELEMENT_ORDER`` — the RFC 5139 schema sequence — not
    in column-declaration order. See that constant for why they differ.
    """
    out: list[tuple[str, str]] = []
    for column, tag in _CIVIC_TAGS:
        value = getattr(erl, column, None)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out.append((tag, text))
    return out


def render_pidf_lo(
    erl: EmergencyResponseLocation,
    *,
    entity: str,
    rule_matched: str | None = None,
    observed_at: datetime | None = None,
    retention_hours: int = 24,
) -> bytes:
    """One ``<presence>`` document for ``erl``.

    ``entity`` is the presentity URI the location is about — a HELD caller
    gets ``pres:`` plus the identity it asked about, which is what lets a
    PBX correlate a response with its request.

    ``observed_at`` becomes the tuple timestamp when the answer rested on
    an observation. A config-derived answer has none, and rather than
    inventing one we stamp the current time and say ``Manual`` in the
    method — claiming a device was *seen* at a location we only configured
    would be the same lie the resolver's ``confidence`` field exists to
    avoid.
    """
    _register_prefixes()

    presence = ET.Element(f"{{{NS_PIDF}}}presence", {"entity": entity})
    tuple_el = ET.SubElement(presence, f"{{{NS_PIDF}}}tuple", {"id": _uuid.uuid4().hex[:12]})
    status = ET.SubElement(tuple_el, f"{{{NS_PIDF}}}status")
    geopriv = ET.SubElement(status, f"{{{NS_GEOPRIV}}}geopriv")
    location_info = ET.SubElement(geopriv, f"{{{NS_GEOPRIV}}}location-info")

    pairs = civic_element_pairs(erl)
    if pairs:
        civic = ET.SubElement(location_info, f"{{{NS_CIVIC}}}civicAddress")
        for tag, value in pairs:
            ET.SubElement(civic, f"{{{NS_CIVIC}}}{tag}").text = value

    if erl.latitude is not None and erl.longitude is not None:
        point = ET.SubElement(location_info, f"{{{NS_GML}}}Point", {"srsName": SRS_NAME})
        # Latitude then longitude, which is EPSG:4326 axis order. Emitting
        # them the other way round is the classic way to put a location in
        # the sea, and it parses perfectly.
        ET.SubElement(point, f"{{{NS_GML}}}pos").text = f"{erl.latitude} {erl.longitude}"

    rules = ET.SubElement(geopriv, f"{{{NS_GEOPRIV}}}usage-rules")
    ET.SubElement(rules, f"{{{NS_BASIC_POLICY}}}retransmission-allowed").text = "no"
    expiry = datetime.now(UTC).replace(microsecond=0) + _hours(retention_hours)
    ET.SubElement(rules, f"{{{NS_BASIC_POLICY}}}retention-expiry").text = _rfc3339(expiry)

    ET.SubElement(geopriv, f"{{{NS_GEOPRIV}}}method").text = RULE_METHOD.get(
        rule_matched or "", "Manual"
    )

    ET.SubElement(tuple_el, f"{{{NS_PIDF}}}timestamp").text = _rfc3339(
        observed_at or datetime.now(UTC)
    )

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        presence, encoding="utf-8", xml_declaration=False
    )


def _hours(n: int):
    from datetime import timedelta

    return timedelta(hours=max(0, n))


def _rfc3339(dt: datetime) -> str:
    """RFC 3339 with a ``Z``, to three decimal places or none.

    Matches what the rest of the API emits (#907) rather than
    ``isoformat()``'s six digits, so a consumer that decodes our JSON
    timestamps decodes these too.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
