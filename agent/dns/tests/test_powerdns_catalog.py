"""Tests for the PowerDNS catalog-zone renderer (issue #127, Phase 3d)."""

from __future__ import annotations

import hashlib

from spatium_dns_agent.drivers.powerdns import _render_catalog_zone_payload


def _sha1_label(zone: str) -> str:
    """Reproduce the RFC 9432 §4.1 SHA-1 wire-format zone-name hash."""
    text = zone.lower().rstrip(".")
    wire = (
        b"".join(
            bytes([len(label)]) + label.encode("ascii")
            for label in text.split(".")
            if label
        )
        + b"\x00"
    )
    return hashlib.sha1(wire).hexdigest()


def test_catalog_zone_payload_apex_records() -> None:
    payload = _render_catalog_zone_payload(
        {
            "zone_name": "catalog.spatium.invalid.",
            "members": [],
        }
    )
    assert payload["name"] == "catalog.spatium.invalid."
    assert payload["kind"] == "Native"

    rrsets = {(r["name"], r["type"]): r for r in payload["rrsets"]}

    # Apex SOA — invalid placeholder owner per RFC 9432 (catalog
    # zones are administrative, not authoritative for real names).
    soa = rrsets[("catalog.spatium.invalid.", "SOA")]
    assert "invalid. invalid." in soa["records"][0]["content"]

    # Apex NS — also invalid placeholder.
    ns = rrsets[("catalog.spatium.invalid.", "NS")]
    assert ns["records"][0]["content"] == "invalid."

    # version.<catalog>. IN TXT "2" — pinned schema version.
    version = rrsets[("version.catalog.spatium.invalid.", "TXT")]
    assert version["records"][0]["content"] == '"2"'


def test_catalog_zone_payload_member_ptr_uses_rfc_9432_hash() -> None:
    members = [
        {"zone_name": "example.com."},
        {"zone_name": "example.org."},
    ]
    payload = _render_catalog_zone_payload(
        {
            "zone_name": "catalog.spatium.invalid.",
            "members": members,
        }
    )

    # Every member produces exactly one PTR rrset under
    # ``<sha1>.zones.<catalog>.``. Hashes must be deterministic and
    # match a hand-computed reference.
    ptrs = {
        r["records"][0]["content"]: r
        for r in payload["rrsets"]
        if r["type"] == "PTR"
    }
    assert "example.com." in ptrs
    assert "example.org." in ptrs

    expected_label = (
        f"{_sha1_label('example.com')}.zones.catalog.spatium.invalid."
    )
    assert ptrs["example.com."]["name"] == expected_label


def test_catalog_zone_payload_skips_empty_member_names() -> None:
    payload = _render_catalog_zone_payload(
        {
            "zone_name": "catalog.spatium.invalid.",
            "members": [
                {"zone_name": ""},
                {"zone_name": None},
                {"zone_name": "real.example."},
            ],
        }
    )
    ptr_targets = [
        r["records"][0]["content"]
        for r in payload["rrsets"]
        if r["type"] == "PTR"
    ]
    assert ptr_targets == ["real.example."]


def test_catalog_zone_payload_normalises_zone_name_trailing_dot() -> None:
    # Operators sometimes type the catalog zone name without a
    # trailing dot. The renderer must still emit FQDNs with the dot
    # so PowerDNS accepts the records on POST.
    payload = _render_catalog_zone_payload(
        {
            "zone_name": "catalog.spatium.invalid",
            "members": [],
        }
    )
    assert payload["name"] == "catalog.spatium.invalid."
    rrsets = {(r["name"], r["type"]) for r in payload["rrsets"]}
    assert ("catalog.spatium.invalid.", "SOA") in rrsets
    assert ("version.catalog.spatium.invalid.", "TXT") in rrsets


def test_catalog_zone_payload_serial_is_unix_timestamp() -> None:
    # Each render bumps the serial via int(time.time()) so consumers
    # always pull on membership change. Just validate the field is
    # present and a sane unix timestamp (>= 2026-01-01).
    payload = _render_catalog_zone_payload(
        {
            "zone_name": "catalog.spatium.invalid.",
            "members": [],
        }
    )
    assert payload["serial"] >= 1_767_225_600  # 2026-01-01 UTC
