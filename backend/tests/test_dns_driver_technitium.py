"""Unit tests for the Technitium DNS Server driver (v1 — primary zones +
standard record CRUD)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.drivers.dns import get_driver
from app.drivers.dns.base import (
    BlocklistEntry,
    ConfigBundle,
    EffectiveBlocklistData,
    RecordData,
    ServerOptions,
    ViewData,
    ZoneData,
)
from app.drivers.dns.technitium import TechnitiumDriver


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
        serial=2026072701,
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
    return ConfigBundle(
        server_id=str(uuid.uuid4()),
        server_name="technitium1",
        driver="technitium",
        roles=("authoritative",),
        options=ServerOptions(),
        acls=(),
        views=(),
        zones=(zone,),
        tsig_keys=(),
        blocklists=(),
        generated_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )


# ── Registry ──────────────────────────────────────────────────────────────


def test_registry_returns_technitium() -> None:
    drv = get_driver("technitium")
    assert isinstance(drv, TechnitiumDriver)
    assert drv.capabilities()["name"] == "technitium"


# ── Rendering ─────────────────────────────────────────────────────────────


def test_render_server_config_redacts_token() -> None:
    out = TechnitiumDriver().render_server_config(None, ServerOptions())
    payload = json.loads(out)
    assert payload["api_token"] == "<agent-generated>"


def test_render_zone_config_returns_empty_string(zone: ZoneData) -> None:
    # Technitium stores zones in its own internal store, not in per-zone
    # config stanzas. The driver returns an empty string by design.
    assert TechnitiumDriver().render_zone_config(zone) == ""


def test_render_zone_file_emits_technitium_api_payload(
    zone: ZoneData, records: list[RecordData]
) -> None:
    out = TechnitiumDriver().render_zone_file(zone, records)
    payload = json.loads(out)

    assert payload["zone"] == "example.com"
    assert payload["type"] == "Primary"

    by_domain_type = {(r["domain"], r["type"]): r for r in payload["records"]}

    mx = by_domain_type[("example.com", "MX")]
    assert mx["exchange"] == "mail.example.com"
    assert mx["preference"] == 10

    srv = by_domain_type[("_sip._tcp.example.com", "SRV")]
    assert srv["target"] == "sip.example.com"
    assert srv["priority"] == 10
    assert srv["weight"] == 20
    assert srv["port"] == 5060

    txt = by_domain_type[("example.com", "TXT")]
    assert txt["text"] == "v=spf1 -all"

    a = by_domain_type[("www.example.com", "A")]
    assert a["ipAddress"] == "10.0.0.1"

    cname = by_domain_type[("alias.example.com", "CNAME")]
    assert cname["cname"] == "www.example.com"


def test_render_zone_file_is_deterministic(zone: ZoneData, records: list[RecordData]) -> None:
    out1 = TechnitiumDriver().render_zone_file(zone, records)
    out2 = TechnitiumDriver().render_zone_file(zone, records)
    assert out1 == out2


def test_render_rpz_zone_returns_empty_string() -> None:
    bl = EffectiveBlocklistData(
        rpz_zone_name="spatium-blocklist.rpz.",
        entries=(
            BlocklistEntry(
                domain="ads.example.org",
                action="block",
                block_mode="nxdomain",
                sinkhole_ip=None,
                target=None,
                is_wildcard=False,
            ),
        ),
        exceptions=frozenset(),
    )
    assert TechnitiumDriver().render_rpz_zone(bl) == ""


# ── Validation ────────────────────────────────────────────────────────────


def test_validate_config_ok(bundle: ConfigBundle) -> None:
    ok, errors = TechnitiumDriver().validate_config(bundle)
    assert ok is True
    assert errors == []


def test_validate_config_rejects_views(bundle: ConfigBundle) -> None:
    bundle.views = (
        ViewData(
            name="internal",
            match_clients=("10.0.0.0/8",),
            match_destinations=(),
            recursion=False,
            order=1,
        ),
    )
    ok, errors = TechnitiumDriver().validate_config(bundle)
    assert ok is False
    assert any("does not support views" in e for e in errors)


def test_validate_config_rejects_non_primary_zone(zone: ZoneData) -> None:
    secondary = ZoneData(
        name=zone.name,
        zone_type="secondary",
        kind=zone.kind,
        ttl=zone.ttl,
        refresh=zone.refresh,
        retry=zone.retry,
        expire=zone.expire,
        minimum=zone.minimum,
        primary_ns=zone.primary_ns,
        admin_email=zone.admin_email,
        serial=zone.serial,
        masters=("10.0.0.1",),
    )
    bundle = ConfigBundle(
        server_id=str(uuid.uuid4()),
        server_name="technitium1",
        driver="technitium",
        roles=("authoritative",),
        options=ServerOptions(),
        acls=(),
        views=(),
        zones=(secondary,),
        tsig_keys=(),
        blocklists=(),
        generated_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    ok, errors = TechnitiumDriver().validate_config(bundle)
    assert ok is False
    assert any("not supported by the v1 Technitium driver" in e for e in errors)


def test_validate_config_rejects_unsupported_record_types(zone: ZoneData) -> None:
    bad = ZoneData(
        name=zone.name,
        zone_type="primary",
        kind=zone.kind,
        ttl=zone.ttl,
        refresh=zone.refresh,
        retry=zone.retry,
        expire=zone.expire,
        minimum=zone.minimum,
        primary_ns=zone.primary_ns,
        admin_email=zone.admin_email,
        serial=zone.serial,
        records=(
            # ALIAS is a PowerDNS-only record type; Technitium has no
            # equivalent (it has ANAME, a different shape) — stand-in for
            # "not in our v1 supported set".
            RecordData(name="@", record_type="ALIAS", value="lb.example.net.", ttl=60),
        ),
    )
    bundle = ConfigBundle(
        server_id=str(uuid.uuid4()),
        server_name="technitium1",
        driver="technitium",
        roles=("authoritative",),
        options=ServerOptions(),
        acls=(),
        views=(),
        zones=(bad,),
        tsig_keys=(),
        blocklists=(),
        generated_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    ok, errors = TechnitiumDriver().validate_config(bundle)
    assert ok is False
    assert any("ALIAS" in e for e in errors)


def test_validate_config_accepts_modern_record_types(zone: ZoneData) -> None:
    modern = ZoneData(
        name=zone.name,
        zone_type="primary",
        kind=zone.kind,
        ttl=zone.ttl,
        refresh=zone.refresh,
        retry=zone.retry,
        expire=zone.expire,
        minimum=zone.minimum,
        primary_ns=zone.primary_ns,
        admin_email=zone.admin_email,
        serial=zone.serial,
        records=(
            RecordData(name="_443._tcp", record_type="TLSA", value="3 1 1 abcd", ttl=300),
            RecordData(name="@", record_type="HTTPS", value="1 . alpn=h2", ttl=300),
            RecordData(name="@", record_type="SVCB", value="1 target.example.com.", ttl=300),
        ),
    )
    bundle = ConfigBundle(
        server_id=str(uuid.uuid4()),
        server_name="technitium1",
        driver="technitium",
        roles=("authoritative",),
        options=ServerOptions(),
        acls=(),
        views=(),
        zones=(modern,),
        tsig_keys=(),
        blocklists=(),
        generated_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    ok, errors = TechnitiumDriver().validate_config(bundle)
    assert ok is True
    assert errors == []


def test_validate_config_blocklists_are_warned_not_errored(zone: ZoneData) -> None:
    bl = EffectiveBlocklistData(
        rpz_zone_name="spatium-blocklist.rpz.",
        entries=(),
        exceptions=frozenset(),
    )
    bundle = ConfigBundle(
        server_id=str(uuid.uuid4()),
        server_name="technitium1",
        driver="technitium",
        roles=("authoritative",),
        options=ServerOptions(),
        acls=(),
        views=(),
        zones=(zone,),
        tsig_keys=(),
        blocklists=(bl,),
        generated_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    ok, errors = TechnitiumDriver().validate_config(bundle)
    assert ok is True
    assert errors == []


# ── Capabilities ──────────────────────────────────────────────────────────


def test_capabilities_v1_scope() -> None:
    caps = TechnitiumDriver().capabilities()
    assert caps["zone_types"] == ["primary"]
    assert caps["dnssec_inline_signing"] is False
    assert caps["alias_records"] is False
    assert caps["lua_records"] is False
    assert caps["catalog_zones"] is False
    assert "SVCB" in caps["record_types"]
    assert "HTTPS" in caps["record_types"]
    assert "DNAME" in caps["record_types"]


def test_dynamic_update_caps_unsupported() -> None:
    caps = TechnitiumDriver().dynamic_update_caps
    assert caps.supports_ip_acl is False
    assert caps.supports_tsig_acl is False
