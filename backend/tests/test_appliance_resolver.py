"""Service-layer tests for the appliance DNS resolver bundle (issue #158).

Covers:

* automatic mode → disabled-shape (empty body / hash).
* override mode renders DNS= / FallbackDNS= / Domains=~. / DNSSEC= /
  DNSOverTLS=.
* deterministic config_hash; changing the server list shifts it.
* NEVER emits DNSStubListener (the image's no-stub-listener.conf owns it).
* empty servers in override still produce a valid [Resolve] body.
"""

from __future__ import annotations

from app.models.settings import PlatformSettings
from app.services.appliance.resolver import render_resolved_conf, resolver_bundle


def test_automatic_mode_disabled_shape() -> None:
    s = PlatformSettings(id=1, resolver_mode="automatic")
    block = resolver_bundle(s)
    assert block == {"enabled": False, "config_hash": "", "resolved_conf": ""}


def test_override_renders_all_directives() -> None:
    s = PlatformSettings(
        id=1,
        resolver_mode="override",
        resolver_servers=["1.1.1.1", "9.9.9.9"],
        resolver_fallback_servers=["8.8.8.8"],
        resolver_search_domains=["corp.example.com", "lab.example.com"],
        resolver_dnssec="yes",
        resolver_dns_over_tls="opportunistic",
    )
    block = resolver_bundle(s)
    assert block["enabled"] is True
    assert block["config_hash"]  # non-empty when enabled
    body = block["resolved_conf"]
    assert "[Resolve]" in body
    assert "DNS=1.1.1.1 9.9.9.9" in body
    assert "FallbackDNS=8.8.8.8" in body
    # Route-only default domain FIRST so the global servers win, then search.
    assert "Domains=~. corp.example.com lab.example.com" in body
    assert "DNSSEC=yes" in body
    assert "DNSOverTLS=opportunistic" in body


def test_never_emits_dns_stub_listener() -> None:
    # The image-shipped no-stub-listener.conf owns DNSStubListener; this
    # drop-in must NEVER touch it (BIND9 binds host :53).
    s = PlatformSettings(
        id=1,
        resolver_mode="override",
        resolver_servers=["1.1.1.1"],
    )
    body = render_resolved_conf(s)
    assert "DNSStubListener" not in body
    # And the bundle body too.
    assert "DNSStubListener" not in resolver_bundle(s)["resolved_conf"]


def test_deterministic_hash() -> None:
    s = PlatformSettings(id=1, resolver_mode="override", resolver_servers=["1.1.1.1"])
    assert resolver_bundle(s)["config_hash"] == resolver_bundle(s)["config_hash"]


def test_server_change_shifts_hash() -> None:
    s = PlatformSettings(id=1, resolver_mode="override", resolver_servers=["1.1.1.1"])
    before = resolver_bundle(s)["config_hash"]
    s.resolver_servers = ["9.9.9.9"]
    after = resolver_bundle(s)["config_hash"]
    assert before != after


def test_override_empty_servers_still_valid() -> None:
    # Override mode with no servers is still a valid [Resolve] body — the
    # renderer documents the half-configured state but never crashes, and
    # the route-only default + DNSSEC/DoT lines are still emitted.
    s = PlatformSettings(id=1, resolver_mode="override", resolver_servers=[])
    block = resolver_bundle(s)
    assert block["enabled"] is True
    body = block["resolved_conf"]
    assert "[Resolve]" in body
    assert "Domains=~." in body
    # Defaults applied.
    assert "DNSSEC=allow-downgrade" in body
    assert "DNSOverTLS=no" in body
    # No DNS= line with content, but a helpful comment instead.
    assert "No upstream DNS servers configured" in body
