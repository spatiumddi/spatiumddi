"""Technitium agent driver — pure helper unit tests (param builders, name
qualification, SVCB parsing). No live daemon required.

Live-API behavior (auth header shape, idempotent-error detection, zone
create / record add / delete / reconcile semantics) was verified empirically
against a real ``technitium/dns-server`` container during development — see
the driver module's docstring for the confirmed API shapes. Those call
sites (``_call``, ``_create_api_token``, ``_reconcile_zones``) build a fresh
``httpx.Client`` per call and are exercised by the docker-compose live test
in Phase 3 rather than mocked here.
"""

from __future__ import annotations

from pathlib import Path

from spatium_dns_agent.drivers.technitium import (
    TechnitiumDriver,
    _qualified_name,
    _record_params,
    _svcb_params,
)


def test_qualified_name_apex() -> None:
    assert _qualified_name("example.com", "@") == "example.com"
    assert _qualified_name("example.com", "") == "example.com"
    assert _qualified_name("example.com.", "example.com.") == "example.com"


def test_qualified_name_subdomain() -> None:
    assert _qualified_name("example.com", "www") == "www.example.com"
    assert _qualified_name("example.com", "_sip._tcp") == "_sip._tcp.example.com"


def test_record_params_a_aaaa() -> None:
    assert _record_params("A", "10.0.0.1", {}) == {"ipAddress": "10.0.0.1"}
    assert _record_params("AAAA", "2001:db8::1", {}) == {"ipAddress": "2001:db8::1"}


def test_record_params_cname_dname_ns_ptr() -> None:
    assert _record_params("CNAME", "www.example.com.", {}) == {"cname": "www.example.com"}
    assert _record_params("DNAME", "other.example.net.", {}) == {"dname": "other.example.net"}
    assert _record_params("NS", "ns1.example.com.", {}) == {"nameServer": "ns1.example.com"}
    assert _record_params("PTR", "host.example.com.", {}) == {"ptrName": "host.example.com"}


def test_record_params_mx_uses_priority() -> None:
    rec = {"priority": 20}
    assert _record_params("MX", "mail.example.com.", rec) == {
        "exchange": "mail.example.com",
        "preference": 20,
    }


def test_record_params_mx_default_priority() -> None:
    assert _record_params("MX", "mail.example.com.", {})["preference"] == 10


def test_record_params_srv() -> None:
    rec = {"priority": 10, "weight": 20, "port": 5060}
    assert _record_params("SRV", "sip.example.com.", rec) == {
        "target": "sip.example.com",
        "priority": 10,
        "weight": 20,
        "port": 5060,
    }


def test_record_params_txt() -> None:
    assert _record_params("TXT", "v=spf1 -all", {}) == {"text": "v=spf1 -all"}


def test_record_params_caa() -> None:
    out = _record_params("CAA", '0 issue "letsencrypt.org"', {})
    assert out == {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}


def test_record_params_tlsa() -> None:
    out = _record_params("TLSA", "3 1 1 abcd1234", {})
    assert out == {
        "tlsaCertificateUsage": "3",
        "tlsaSelector": "1",
        "tlsaMatchingType": "1",
        "tlsaCertificateAssociationData": "abcd1234",
    }


def test_record_params_sshfp() -> None:
    out = _record_params("SSHFP", "4 2 abcd1234", {})
    assert out == {
        "sshfpAlgorithm": "4",
        "sshfpFingerprintType": "2",
        "sshfpFingerprint": "abcd1234",
    }


def test_record_params_naptr() -> None:
    value = '100 10 U "E2U+sip" "!^.*$!sip:info@example.com!" .'
    out = _record_params("NAPTR", value, {})
    assert out["naptrOrder"] == "100"
    assert out["naptrPreference"] == "10"
    assert out["naptrFlags"] == "U"
    assert out["naptrServices"] == "E2U+sip"
    assert out["naptrReplacement"] == "."


def test_record_params_uri() -> None:
    out = _record_params("URI", "1 1 https://example.com", {})
    assert out == {"uriPriority": "1", "uriWeight": "1", "uri": "https://example.com"}


def test_svcb_params_single_value() -> None:
    priority, target, params = _svcb_params('1 . alpn="h2"')
    assert priority == 1
    assert target == "."
    assert params == "alpn|h2"


def test_svcb_params_no_params() -> None:
    priority, target, params = _svcb_params("1 svc.example.com.")
    assert priority == 1
    assert target == "svc.example.com."
    assert params == ""


def test_svcb_params_multivalue_truncates_first() -> None:
    priority, target, params = _svcb_params('1 . alpn="h2,h3"')
    assert params == "alpn|h2"


def test_record_params_svcb() -> None:
    out = _record_params("SVCB", '1 . alpn="h2"', {})
    assert out == {"svcPriority": 1, "svcTargetName": ".", "svcParams": "alpn|h2"}


def test_record_params_https_no_params_omits_svcparams() -> None:
    out = _record_params("HTTPS", "1 .", {})
    assert out == {"svcPriority": 1, "svcTargetName": "."}


def test_admin_bootstrap_password_persists(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    pw1 = d.admin_bootstrap_password()
    pw2 = d.admin_bootstrap_password()
    assert pw1 == pw2
    path = tmp_path / "technitium-admin-password"
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_render_skips_daemon_managed_apex_types(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    bundle = {
        "zones": [
            {
                "name": "example.com.",
                "type": "primary",
                "ttl": 3600,
                "records": [
                    {"name": "@", "type": "SOA", "value": "ignored"},
                    {"name": "@", "type": "NS", "value": "ns1.example.com."},
                    {"name": "sub", "type": "NS", "value": "ns2.example.com."},
                    {"name": "www", "type": "A", "value": "10.0.0.1", "ttl": 300},
                ],
            }
        ]
    }
    d.render(bundle)
    import json

    payload = json.loads((tmp_path / "rendered.new" / "zones.json").read_text())
    zone = payload[0]
    types_by_domain = {(r["domain"], r["type"]) for r in zone["records"]}
    # Apex SOA + apex NS are daemon-managed — excluded.
    assert ("example.com", "SOA") not in types_by_domain
    assert ("example.com", "NS") not in types_by_domain
    # Off-apex NS (delegation) and ordinary records pass through.
    assert ("sub.example.com", "NS") in types_by_domain
    assert ("www.example.com", "A") in types_by_domain


def test_render_skips_non_primary_zones(tmp_path: Path) -> None:
    d = TechnitiumDriver(state_dir=tmp_path)
    bundle = {
        "zones": [
            {"name": "secondary.example.com.", "type": "secondary", "records": []},
        ]
    }
    d.render(bundle)
    import json

    payload = json.loads((tmp_path / "rendered.new" / "zones.json").read_text())
    assert payload == []
