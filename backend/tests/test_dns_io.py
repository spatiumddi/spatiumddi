"""Unit tests for the DNS zone-file parse / diff / write service."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.dns_io import (
    ZoneParseError,
    diff_records,
    parse_zone_file,
    write_zone_file,
)

REALISTIC_ZONE = """\
$ORIGIN example.com.
$TTL 3600
@\tIN\tSOA\tns1.example.com. hostmaster.example.com. (
    2024010101  ; serial
    86400       ; refresh
    7200        ; retry
    3600000     ; expire
    3600        ; minimum
)
@       IN  NS      ns1.example.com.
@       IN  NS      ns2.example.com.
@       IN  A       192.0.2.1
@       IN  AAAA    2001:db8::1
@       IN  MX  10  mail.example.com.
www     IN  A       192.0.2.10
www     IN  AAAA    2001:db8::10
ftp     IN  CNAME   www.example.com.
_sip._tcp IN SRV    10 60 5060 sipserver.example.com.
txt     IN  TXT     "v=spf1 -all"
"""


def test_parse_realistic_zone() -> None:
    parsed = parse_zone_file(REALISTIC_ZONE, "example.com")
    assert parsed.soa is not None
    assert parsed.soa.serial == 2024010101
    assert parsed.soa.refresh == 86400

    kinds = {r.record_type for r in parsed.records}
    assert {"A", "AAAA", "NS", "MX", "CNAME", "SRV", "TXT"}.issubset(kinds)

    # MX has priority split out
    mx = next(r for r in parsed.records if r.record_type == "MX")
    assert mx.priority == 10
    assert mx.value.startswith("mail.example.com")

    # SRV has priority/weight/port
    srv = next(r for r in parsed.records if r.record_type == "SRV")
    assert (srv.priority, srv.weight, srv.port) == (10, 60, 5060)
    assert "sipserver" in srv.value


def test_parse_malformed_zone_raises_readable_error() -> None:
    with pytest.raises(ZoneParseError) as exc_info:
        parse_zone_file("this is not a zone file {{{", "example.com")
    assert "parse" in str(exc_info.value).lower() or "fail" in str(exc_info.value).lower()


@dataclass
class FakeRecord:
    """Shim mimicking the subset of DNSRecord fields used by diff/write."""

    id: str
    name: str
    record_type: str
    value: str
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None


def test_diff_detects_create_update_delete() -> None:
    parsed = parse_zone_file(REALISTIC_ZONE, "example.com")

    existing = [
        # Matches -> unchanged
        FakeRecord(id="1", name="www", record_type="A", value="192.0.2.10", ttl=3600),
        # Same name/type/value but different TTL -> update
        FakeRecord(id="2", name="@", record_type="A", value="192.0.2.1", ttl=60),
        # Not in parsed -> delete
        FakeRecord(id="3", name="obsolete", record_type="A", value="10.0.0.1", ttl=3600),
    ]

    diff = diff_records(parsed.records, existing)

    create_keys = {(c.name, c.record_type, c.value) for c in diff.to_create}
    # At minimum AAAA records and TXT should be creates
    assert any(c.record_type == "AAAA" for c in diff.to_create)
    assert ("www", "A", "192.0.2.10") not in create_keys

    updated_pairs = {(c.existing_id, c.record_type) for c in diff.to_update}
    assert ("2", "A") in updated_pairs

    deleted_ids = {c.existing_id for c in diff.to_delete}
    assert "3" in deleted_ids


def test_write_zone_file_round_trip() -> None:
    # Zone stub with fields the writer needs.
    @dataclass
    class FakeZone:
        name: str = "example.com."
        primary_ns: str = "ns1.example.com."
        admin_email: str = "hostmaster.example.com."
        ttl: int = 3600
        refresh: int = 86400
        retry: int = 7200
        expire: int = 3600000
        minimum: int = 3600
        last_serial: int = 2024010101

    zone = FakeZone()
    records = [
        FakeRecord(id="1", name="@", record_type="NS", value="ns1.example.com.", ttl=3600),
        FakeRecord(id="2", name="@", record_type="A", value="192.0.2.1", ttl=3600),
        FakeRecord(id="3", name="www", record_type="A", value="192.0.2.10", ttl=3600),
        FakeRecord(
            id="4", name="@", record_type="MX", value="mail.example.com.", ttl=3600, priority=10
        ),
        FakeRecord(
            id="5",
            name="_sip._tcp",
            record_type="SRV",
            value="sipserver.example.com.",
            ttl=3600,
            priority=10,
            weight=60,
            port=5060,
        ),
        FakeRecord(id="6", name="txt", record_type="TXT", value="v=spf1 -all", ttl=3600),
    ]

    text = write_zone_file(zone, records)  # type: ignore[arg-type]
    assert "$ORIGIN example.com." in text
    assert "SOA" in text
    assert "192.0.2.1" in text
    assert "10 60 5060" in text  # SRV
    assert '"v=spf1 -all"' in text  # TXT quoted

    # Re-parse the output and confirm we get back the same records.
    reparsed = parse_zone_file(text, "example.com")
    values = {(r.name, r.record_type, r.value) for r in reparsed.records}
    assert ("@", "A", "192.0.2.1") in values
    assert ("www", "A", "192.0.2.10") in values
    assert any(r.record_type == "SRV" and r.priority == 10 for r in reparsed.records)
    assert any(r.record_type == "MX" and r.priority == 10 for r in reparsed.records)
