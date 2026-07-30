"""Regression tests for the shared AXFR helper (``app.drivers.dns._axfr``).

This module had no coverage at all, which is how it shipped a bug that made
every hostname-addressed AXFR fail 100% of the time: ``dns.query.xfr`` takes
an IP *literal* and calls ``dns.inet.af_for_address()`` on it up front, so a
``DNSServer.host`` like ``"dns-bind9"`` raised a bare, message-less
``ValueError`` before a single packet was sent. The #61 drift report and the
pull-importer both surfaced that empty string as the failure reason, so the
symptom was "failed, no reason given" on every BIND9 server.

The tests below pin both halves of the fix: names get resolved before the
transfer, and no failure path is ever allowed to produce a blank message.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from app.drivers.dns._axfr import axfr_zone_records


class _FakeZone:
    """Minimal stand-in for ``dns.zone.Zone`` — an empty zone."""

    def items(self) -> list[Any]:
        return []


@pytest.fixture
def captured_xfr(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch out the network so we can assert on what ``xfr`` was handed."""
    import dns.query
    import dns.zone

    seen: dict[str, Any] = {}

    def fake_xfr(where: str, origin: Any, **kwargs: Any) -> object:
        seen["where"] = where
        seen["port"] = kwargs.get("port")
        return object()

    monkeypatch.setattr(dns.query, "xfr", fake_xfr)
    monkeypatch.setattr(dns.zone, "from_xfr", lambda _gen: _FakeZone())
    return seen


async def test_hostname_is_resolved_before_transfer(
    captured_xfr: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostname must reach ``dns.query.xfr`` as an IP, never as a name.

    This is the actual shipped bug: passing the name through unresolved is
    what raised the bare ValueError.
    """
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 53))],
    )

    await axfr_zone_records(host="dns-bind9", port=53, zone_name="example.com.")

    assert captured_xfr["where"] == "192.0.2.10"


@pytest.mark.parametrize("literal", ["192.0.2.10", "2001:db8::1"])
async def test_ip_literals_pass_through_unchanged(
    literal: str, captured_xfr: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An address that already parses must not take the resolver path."""

    def _boom(*_a: Any, **_kw: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("getaddrinfo called for an IP literal")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    await axfr_zone_records(host=literal, port=53, zone_name="example.com.")

    assert captured_xfr["where"] == literal


async def test_resolution_failure_names_the_host(
    captured_xfr: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable host produces an operator-readable error, not OSError."""

    def _fail(*_a: Any, **_kw: Any) -> Any:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)

    with pytest.raises(RuntimeError) as excinfo:
        await axfr_zone_records(host="no-such-host", port=53, zone_name="example.com.")

    msg = str(excinfo.value)
    assert "no-such-host" in msg
    assert "example.com." in msg
    assert "resolve" in msg


async def test_all_resolved_addresses_are_tried(
    captured_xfr: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dual-stack host must fall through to its other addresses.

    The first address a resolver returns isn't necessarily the one
    allow-transfer permits, or even one that's routable from here.
    """
    import dns.query

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 53, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 53)),
        ],
    )

    tried: list[str] = []

    def flaky_xfr(where: str, origin: Any, **kwargs: Any) -> object:
        tried.append(where)
        if where == "2001:db8::1":
            raise OSError("No route to host")
        return object()

    monkeypatch.setattr(dns.query, "xfr", flaky_xfr)

    await axfr_zone_records(host="dual.example.", port=53, zone_name="example.com.")

    assert tried == ["2001:db8::1", "192.0.2.10"]


def test_resolve_dedupes_repeated_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """getaddrinfo repeats an address per socket type; don't try it twice."""
    from app.drivers.dns._axfr import resolve_server_address

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 53)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("192.0.2.10", 53)),
        ],
    )

    assert resolve_server_address("host.example.", 53, what="AXFR of x") == ["192.0.2.10"]


async def test_message_less_exception_never_surfaces_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``ValueError()`` must not reach the caller as an empty string.

    Callers (drift report, pull-importer) render this text verbatim, so a
    blank message means the UI shows "failed" with no reason — the exact
    thing that made the original bug so hard to diagnose.
    """
    import dns.query

    def _bare(*_a: Any, **_kw: Any) -> Any:
        raise ValueError

    monkeypatch.setattr(dns.query, "xfr", _bare)

    with pytest.raises(RuntimeError) as excinfo:
        await axfr_zone_records(host="192.0.2.10", port=53, zone_name="example.com.")

    msg = str(excinfo.value)
    assert msg.strip()
    assert "ValueError" in msg
    assert "192.0.2.10" in msg
