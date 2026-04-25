"""Unit tests for the Tailscale client's parse helpers.

The HTTP layer is covered by ``test_tailscale_reconcile`` via a
fake client; these tests exercise the pure-function helpers.
"""

from __future__ import annotations

from app.services.tailscale.client import (
    _TailscaleDevice,
    derive_tailnet_domain,
)


def test_derive_tailnet_domain_strips_hostname() -> None:
    devices = [
        _TailscaleDevice(id="1", node_id="n1", name="laptop.tail123ab.ts.net", hostname="laptop"),
    ]
    assert derive_tailnet_domain(devices) == "tail123ab.ts.net"


def test_derive_tailnet_domain_handles_org_slug() -> None:
    devices = [
        _TailscaleDevice(
            id="1",
            node_id="n1",
            name="server-1.rooster-trout.ts.net",
            hostname="server-1",
        ),
    ]
    assert derive_tailnet_domain(devices) == "rooster-trout.ts.net"


def test_derive_tailnet_domain_skips_devices_without_fqdn() -> None:
    devices = [
        _TailscaleDevice(id="1", node_id="n1", name="laptop", hostname="laptop"),
        _TailscaleDevice(id="2", node_id="n2", name="phone.example.ts.net", hostname="phone"),
    ]
    assert derive_tailnet_domain(devices) == "example.ts.net"


def test_derive_tailnet_domain_returns_none_when_empty() -> None:
    assert derive_tailnet_domain([]) is None


def test_derive_tailnet_domain_returns_none_when_no_fqdn() -> None:
    devices = [
        _TailscaleDevice(id="1", node_id="n1", name="laptop", hostname="laptop"),
    ]
    assert derive_tailnet_domain(devices) is None


def test_derive_tailnet_domain_strips_trailing_dot() -> None:
    devices = [
        _TailscaleDevice(id="1", node_id="n1", name="host.example.ts.net.", hostname="host"),
    ]
    assert derive_tailnet_domain(devices) == "example.ts.net"
