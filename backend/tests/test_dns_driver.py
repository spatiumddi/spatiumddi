"""Unit tests for the DNS driver abstraction layer and the BIND9 driver."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.drivers.dns import get_driver, register_driver
from app.drivers.dns.base import (
    AclData,
    BlocklistEntry,
    ConfigBundle,
    DNSDriver,
    EffectiveBlocklistData,
    RecordData,
    ServerOptions,
    TrustAnchorData,
    TsigKey,
    ZoneData,
)
from app.drivers.dns.bind9 import BIND9Driver
from app.services.dns.serial import bump_zone_serial, compute_next_serial

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def zone() -> ZoneData:
    return ZoneData(
        name="example.com.",
        zone_type="primary",
        kind="forward",
        ttl=3600,
        refresh=86400,
        retry=7200,
        expire=3600000,
        minimum=3600,
        primary_ns="ns1.example.com.",
        admin_email="hostmaster.example.com.",
        serial=2026041401,
        records=(),
    )


@pytest.fixture
def records() -> list[RecordData]:
    return [
        RecordData(name="@", record_type="NS", value="ns1.example.com.", ttl=3600),
        RecordData(name="www", record_type="A", value="10.0.0.1", ttl=300),
        RecordData(name="ipv6", record_type="AAAA", value="2001:db8::1", ttl=300),
        RecordData(name="alias", record_type="CNAME", value="www.example.com.", ttl=300),
        RecordData(name="@", record_type="MX", value="mail.example.com.", ttl=3600, priority=10),
        RecordData(name="@", record_type="TXT", value="v=spf1 -all", ttl=3600),
        RecordData(
            name="_sip._tcp",
            record_type="SRV",
            value="sip.example.com.",
            ttl=3600,
            priority=10,
            weight=20,
            port=5060,
        ),
    ]


@pytest.fixture
def bundle(zone: ZoneData) -> ConfigBundle:
    opts = ServerOptions(
        forwarders=("1.1.1.1", "9.9.9.9"),
        trust_anchors=(
            TrustAnchorData(
                zone_name=".",
                algorithm=8,
                key_tag=20326,
                public_key="AAAA...",
                is_initial_key=True,
            ),
        ),
    )
    return ConfigBundle(
        server_id=str(uuid.uuid4()),
        server_name="ns1",
        driver="bind9",
        roles=("authoritative",),
        options=opts,
        acls=(AclData(name="trusted", entries=(("10.0.0.0/8", False), ("1.2.3.4", True))),),
        views=(),
        zones=(zone,),
        tsig_keys=(
            TsigKey(
                name="ddns-key.",
                algorithm="hmac-sha256",
                secret="c3VwZXJzZWNyZXRiYXNlNjQ=",
            ),
        ),
        blocklists=(),
        generated_at=datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
    )


# ── Driver registry ──────────────────────────────────────────────────────


def test_registry_returns_bind9() -> None:
    drv = get_driver("bind9")
    assert isinstance(drv, BIND9Driver)
    assert drv.capabilities()["name"] == "bind9"


def test_registry_unknown_driver_raises() -> None:
    with pytest.raises(ValueError):
        get_driver("djbdns")


def test_registry_register_driver_override() -> None:
    class FakeDriver(BIND9Driver):
        name = "fake"

    register_driver("fake-bind", FakeDriver)
    assert isinstance(get_driver("fake-bind"), FakeDriver)


# ── BIND9 rendering ──────────────────────────────────────────────────────


def test_render_zone_config_primary(zone: ZoneData) -> None:
    out = BIND9Driver().render_zone_config(zone)
    assert 'zone "example.com."' in out
    assert "type master;" in out
    assert 'file "/var/cache/bind/zones/example.com.db"' in out


def test_render_zone_file_mixed_records(zone: ZoneData, records: list[RecordData]) -> None:
    out = BIND9Driver().render_zone_file(zone, records)
    assert "$TTL 3600" in out
    assert "example.com. IN SOA ns1.example.com." in out
    assert "2026041401 ; serial" in out
    # Individual record types survive rendering
    assert "www 300 IN A 10.0.0.1" in out
    assert "ipv6 300 IN AAAA 2001:db8::1" in out
    assert "alias 300 IN CNAME www.example.com." in out
    assert "@ 3600 IN MX 10 mail.example.com." in out
    assert '@ 3600 IN TXT "v=spf1 -all"' in out
    assert "_sip._tcp 3600 IN SRV 10 20 5060 sip.example.com." in out


def test_render_zone_file_is_idempotent(zone: ZoneData, records: list[RecordData]) -> None:
    drv = BIND9Driver()
    assert drv.render_zone_file(zone, records) == drv.render_zone_file(zone, records)


def test_render_server_config_includes_acl_and_forwarders(bundle: ConfigBundle) -> None:
    out = BIND9Driver().render_server_config(
        SimpleNamespace(id=bundle.server_id, name=bundle.server_name),
        bundle.options,
        bundle=bundle,
    )
    assert 'acl "trusted"' in out
    assert "10.0.0.0/8;" in out
    assert "!1.2.3.4;" in out
    assert "forwarders { 1.1.1.1; 9.9.9.9; };" in out
    assert "forward first;" in out
    assert 'key "ddns-key."' in out
    assert "algorithm hmac-sha256;" in out
    assert 'zone "example.com."' in out
    # No view wrapping when views=()
    assert 'view "' not in out


# ── RPZ rendering ─────────────────────────────────────────────────────────


def test_render_rpz_zone_nxdomain_and_sinkhole_and_exception() -> None:
    bl = EffectiveBlocklistData(
        rpz_zone_name="spatium-blocklist.rpz.",
        entries=(
            BlocklistEntry(
                domain="ads.example.com",
                action="block",
                block_mode="nxdomain",
                sinkhole_ip=None,
                target=None,
                is_wildcard=False,
            ),
            BlocklistEntry(
                domain="tracker.example.net",
                action="block",
                block_mode="sinkhole",
                sinkhole_ip="10.0.0.250",
                target=None,
                is_wildcard=True,
            ),
            BlocklistEntry(
                domain="redirect.example.org",
                action="redirect",
                block_mode="nxdomain",
                sinkhole_ip=None,
                target="1.2.3.4",
                is_wildcard=False,
            ),
            BlocklistEntry(
                domain="allowed.example.com",
                action="block",
                block_mode="nxdomain",
                sinkhole_ip=None,
                target=None,
                is_wildcard=False,
            ),
        ),
        exceptions=frozenset({"allowed.example.com"}),
    )
    out = BIND9Driver().render_rpz_zone(bl)
    assert "spatium-blocklist.rpz. IN SOA" in out
    assert "ads.example.com IN CNAME ." in out
    assert "*.tracker.example.net IN A 10.0.0.250" in out
    assert "redirect.example.org IN A 1.2.3.4" in out
    assert "allowed.example.com" not in out  # exception excluded


# ── Serial bumping ────────────────────────────────────────────────────────


def test_serial_bump_rfc1912_same_day() -> None:
    now = datetime(2026, 4, 14, tzinfo=UTC)
    assert compute_next_serial(2026041400, now=now) == 2026041401
    assert compute_next_serial(2026041499, now=now) == 2026041500  # monotonic overflow


def test_serial_bump_rfc1912_new_day() -> None:
    now = datetime(2026, 4, 14, tzinfo=UTC)
    # previous day's serial rolls to today base
    assert compute_next_serial(2026041305, now=now) == 2026041400


def test_serial_bump_future_serial_stays_monotonic() -> None:
    now = datetime(2026, 4, 14, tzinfo=UTC)
    # A serial ahead of today (e.g. admin set the clock wrong once) must not decrease
    assert compute_next_serial(2030010100, now=now) == 2030010101


def test_serial_bump_non_rfc_serial_falls_back() -> None:
    now = datetime(2026, 4, 14, tzinfo=UTC)
    assert compute_next_serial(0, now=now) == 2026041400
    assert compute_next_serial(42, now=now) == 2026041400
    assert compute_next_serial(2026041400 + 5, now=now) == 2026041400 + 6


def test_bump_zone_serial_mutates_and_returns() -> None:
    z = SimpleNamespace(last_serial=2026041400)
    now = datetime(2026, 4, 14, tzinfo=UTC)
    nxt = bump_zone_serial(z, now=now)
    assert nxt == 2026041401
    assert z.last_serial == 2026041401


# ── Validation ───────────────────────────────────────────────────────────


def test_validate_config_ok(bundle: ConfigBundle) -> None:
    ok, errs = BIND9Driver().validate_config(bundle)
    assert ok is True
    assert errs == []


def test_validate_config_catches_missing_trailing_dot(bundle: ConfigBundle) -> None:
    bad_zone = ZoneData(
        name="no-dot",
        zone_type="primary",
        kind="forward",
        ttl=60,
        refresh=60,
        retry=60,
        expire=60,
        minimum=60,
        primary_ns="",
        admin_email="",
        serial=1,
    )
    broken = ConfigBundle(
        server_id=bundle.server_id,
        server_name=bundle.server_name,
        driver="bind9",
        roles=bundle.roles,
        options=bundle.options,
        acls=bundle.acls,
        views=bundle.views,
        zones=(bad_zone,),
        tsig_keys=bundle.tsig_keys,
        blocklists=bundle.blocklists,
        generated_at=bundle.generated_at,
    )
    ok, errs = BIND9Driver().validate_config(broken)
    assert ok is False
    assert any("must end with '.'" in e for e in errs)


def test_config_bundle_etag_is_deterministic(bundle: ConfigBundle) -> None:
    e1 = bundle.compute_etag()
    e2 = bundle.compute_etag()
    assert e1 == e2
    assert e1.startswith("sha256:")


def test_driver_is_dnsdriver_subclass() -> None:
    assert issubclass(BIND9Driver, DNSDriver)
