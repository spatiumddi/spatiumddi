"""Self-signed cert SAN coverage incl. the control-plane VIP (#272 Phase 6).

The appliance self-signed bootstrap must put the control-plane VIP in
the cert SANs so a cert served on the floating IP validates, and must
regenerate an existing self-signed cert that doesn't yet cover a
newly-configured VIP. These cover the pure-logic helpers plus a
round-trip parse of the generated cert to prove the VIP lands in the
SubjectAlternativeName extension.
"""

from __future__ import annotations

import ipaddress

from cryptography import x509

from app.services.appliance.bootstrap import (
    _desired_sans,
    _generate_self_signed_cert,
    _parse_extra_sans,
    _sans_cover,
)


def test_parse_extra_sans_ips_and_dns_drop_loopback() -> None:
    assert _parse_extra_sans("192.0.2.10, vip.example.com ,127.0.0.1,, 192.0.2.10") == [
        "192.0.2.10",
        "vip.example.com",
    ]
    assert _parse_extra_sans("") == []


def test_desired_sans_dedupes_preserving_order() -> None:
    assert _desired_sans("host1", ["10.0.0.5", "10.0.0.5"], ["192.0.2.10", "host1"]) == [
        "host1",
        "10.0.0.5",
        "192.0.2.10",
    ]


def test_sans_cover() -> None:
    have = ["host1", "10.0.0.5", "192.0.2.10"]
    assert _sans_cover(have, ["host1", "192.0.2.10"])
    assert not _sans_cover(have, ["host1", "192.0.2.99"])
    # Legacy / missing sans_json covers nothing.
    assert not _sans_cover(None, ["host1"])
    assert _sans_cover(None, [])


def test_generated_cert_carries_vip_in_san() -> None:
    cert_pem, _key_pem, info = _generate_self_signed_cert(
        "appliance1", ["10.0.0.5"], ["192.0.2.241", "vip.example.com"]
    )
    # Reported SANs include host + ips + extras.
    assert info["sans"] == ["appliance1", "10.0.0.5", "192.0.2.241", "vip.example.com"]

    # Parse the real cert back and confirm the VIP is in the SAN ext.
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ip_sans = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    dns_sans = set(san.get_values_for_type(x509.DNSName))
    assert ipaddress.ip_address("192.0.2.241") in {ipaddress.ip_address(s) for s in ip_sans}
    assert ipaddress.ip_address("10.0.0.5") in {ipaddress.ip_address(s) for s in ip_sans}
    assert "vip.example.com" in dns_sans
    assert "appliance1" in dns_sans


def test_generated_cert_no_extras_unchanged_shape() -> None:
    _cert_pem, _key, info = _generate_self_signed_cert("appliance1", ["10.0.0.5"])
    assert info["sans"] == ["appliance1", "10.0.0.5"]
