"""Unit tests for ``get_trusted_client_ip`` (#626).

A client can forge ``X-Forwarded-For`` and, because uvicorn runs with
``--forwarded-allow-ips *``, that forged value becomes ``request.client.host``.
The shipped nginx sets ``X-Real-IP: $remote_addr`` as an overwrite the client
cannot influence, so security decisions must prefer it. These tests pin that
contract: a spoofed XFF/peer is ignored whenever a valid ``X-Real-IP`` is
present, and the peer address is used only as a no-nginx fallback.
"""

from __future__ import annotations

from starlette.requests import Request

from app.core.request_meta import client_ip, get_trusted_client_ip


def _make_request(
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("10.0.0.9", 4444),
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict = {"type": "http", "headers": raw_headers, "client": client}
    return Request(scope)


def test_prefers_x_real_ip_over_spoofed_peer() -> None:
    # nginx topology: X-Real-IP is the real peer; the attacker-controlled
    # X-Forwarded-For has already poisoned request.client.host.
    req = _make_request(
        headers={"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4"},
        client=("1.2.3.4", 5555),  # what uvicorn resolved from the forged XFF
    )
    assert get_trusted_client_ip(req) == "203.0.113.7"
    # The raw helper still returns the spoofable value — proving the two differ.
    assert client_ip(req) == "1.2.3.4"


def test_spoofed_x_real_ip_is_overwritten_in_practice() -> None:
    # A client that sends its own X-Real-IP is irrelevant once nginx overwrites
    # it; from the app's side we simply trust the single header value present.
    # This test documents that a lone X-Real-IP wins over the peer regardless.
    req = _make_request(headers={"x-real-ip": "198.51.100.2"}, client=("10.9.9.9", 1))
    assert get_trusted_client_ip(req) == "198.51.100.2"


def test_falls_back_to_peer_without_x_real_ip() -> None:
    # Direct-to-uvicorn (no nginx) deployment: no X-Real-IP header, so the peer
    # address is used — there is no untrusted proxy in front to spoof past.
    req = _make_request(headers={}, client=("192.0.2.55", 22))
    assert get_trusted_client_ip(req) == "192.0.2.55"


def test_malformed_x_real_ip_falls_back_to_peer() -> None:
    # A present-but-garbage X-Real-IP must not poison a downstream allowlist /
    # rate-limit key — fall back to the peer instead.
    req = _make_request(headers={"x-real-ip": "not-an-ip"}, client=("192.0.2.77", 22))
    assert get_trusted_client_ip(req) == "192.0.2.77"


def test_ipv6_x_real_ip_is_accepted() -> None:
    req = _make_request(headers={"x-real-ip": "2001:db8::1"}, client=("10.0.0.1", 1))
    assert get_trusted_client_ip(req) == "2001:db8::1"


def test_no_client_and_no_header_is_none() -> None:
    req = _make_request(headers={}, client=None)
    assert get_trusted_client_ip(req) is None


def test_blank_x_real_ip_falls_back() -> None:
    req = _make_request(headers={"x-real-ip": "   "}, client=("192.0.2.9", 7))
    assert get_trusted_client_ip(req) == "192.0.2.9"
