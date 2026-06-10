"""Hermetic tests for the socket-based network tools (#58).

No real sockets are opened. ``asyncio.open_connection`` /
``create_datagram_endpoint`` are mocked so the port-test classification
logic is exercised deterministically; the TLS parser is fed a
synthetic DER built in-memory with ``cryptography``.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.services.nettools import socket_tools
from app.services.nettools.socket_tools import _parse_cert
from app.services.nettools.socket_tools import test_port as _test_port

# ── port test classification ────────────────────────────────────────


async def test_tcp_open() -> None:
    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    with patch.object(
        socket_tools.asyncio,
        "open_connection",
        AsyncMock(return_value=(MagicMock(), writer)),
    ):
        res = await _test_port("10.0.0.1", 443, "tcp", timeout=1.0)
    assert res.state == "open"
    assert res.rtt_ms is not None


async def test_tcp_closed_on_refused() -> None:
    with patch.object(
        socket_tools.asyncio,
        "open_connection",
        AsyncMock(side_effect=ConnectionRefusedError()),
    ):
        res = await _test_port("10.0.0.1", 9999, "tcp", timeout=1.0)
    assert res.state == "closed"


async def test_tcp_filtered_on_timeout() -> None:
    with patch.object(
        socket_tools.asyncio,
        "wait_for",
        AsyncMock(side_effect=TimeoutError()),
    ):
        res = await _test_port("10.0.0.1", 443, "tcp", timeout=0.5)
    assert res.state == "filtered"
    assert res.error is not None


async def test_udp_open_or_filtered_default() -> None:
    # No ICMP unreachable surfaces → the inherent UDP ambiguity.
    transport = MagicMock()
    proto = MagicMock()
    proto.error = None
    with (
        patch.object(
            socket_tools.asyncio,
            "wait_for",
            AsyncMock(return_value=(transport, proto)),
        ),
        patch.object(socket_tools.asyncio, "sleep", AsyncMock()),
    ):
        res = await _test_port("10.0.0.1", 53, "udp", timeout=0.5)
    assert res.state == "open|filtered"


async def test_udp_closed_on_icmp_unreachable() -> None:
    transport = MagicMock()
    proto = MagicMock()
    proto.error = ConnectionRefusedError()
    with (
        patch.object(
            socket_tools.asyncio,
            "wait_for",
            AsyncMock(return_value=(transport, proto)),
        ),
        patch.object(socket_tools.asyncio, "sleep", AsyncMock()),
    ):
        res = await _test_port("10.0.0.1", 53, "udp", timeout=0.5)
    assert res.state == "closed"


# ── TLS cert parsing ────────────────────────────────────────────────


def _make_cert(
    *,
    common_name: str,
    sans: list[str],
    not_before: dt.datetime,
    not_after: dt.datetime,
    issuer_cn: str | None = None,
) -> bytes:
    """Build a self-signed-ish DER cert in memory. When ``issuer_cn`` is
    given it differs from the subject so the cert reads as CA-signed."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn or common_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]),
            critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.DER)


def test_parse_valid_self_signed() -> None:
    now = dt.datetime.now(dt.UTC)
    der = _make_cert(
        common_name="self.example.com",
        sans=["self.example.com", "alt.example.com"],
        not_before=now - dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=90),
    )
    parsed = _parse_cert(der, "self.example.com")
    assert "self.example.com" in parsed["subject"]  # type: ignore[operator]
    assert parsed["self_signed"] is True
    assert "alt.example.com" in parsed["san"]  # type: ignore[operator]
    assert parsed["expired"] is False
    assert parsed["hostname_matches"] is True
    assert isinstance(parsed["days_remaining"], int) and parsed["days_remaining"] > 80
    assert parsed["signature_algorithm"] == "sha256"


def test_parse_ca_signed_not_self_signed() -> None:
    now = dt.datetime.now(dt.UTC)
    der = _make_cert(
        common_name="www.example.com",
        sans=["www.example.com"],
        not_before=now - dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=30),
        issuer_cn="Lets Encrypt R3",
    )
    parsed = _parse_cert(der, "www.example.com")
    assert parsed["self_signed"] is False
    assert "Lets Encrypt R3" in parsed["issuer"]  # type: ignore[operator]


def test_parse_expired() -> None:
    now = dt.datetime.now(dt.UTC)
    der = _make_cert(
        common_name="old.example.com",
        sans=["old.example.com"],
        not_before=now - dt.timedelta(days=400),
        not_after=now - dt.timedelta(days=10),
    )
    parsed = _parse_cert(der, "old.example.com")
    assert parsed["expired"] is True
    assert parsed["days_remaining"] < 0  # type: ignore[operator]


def test_parse_hostname_mismatch() -> None:
    now = dt.datetime.now(dt.UTC)
    der = _make_cert(
        common_name="real.example.com",
        sans=["real.example.com"],
        not_before=now - dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=30),
    )
    parsed = _parse_cert(der, "imposter.example.com")
    assert parsed["hostname_matches"] is False


def test_parse_wildcard_san_matches() -> None:
    now = dt.datetime.now(dt.UTC)
    der = _make_cert(
        common_name="*.example.com",
        sans=["*.example.com"],
        not_before=now - dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=30),
    )
    parsed = _parse_cert(der, "api.example.com")
    assert parsed["hostname_matches"] is True
    # but not the bare apex (wildcard is exactly one label)
    parsed2 = _parse_cert(der, "example.com")
    assert parsed2["hostname_matches"] is False
