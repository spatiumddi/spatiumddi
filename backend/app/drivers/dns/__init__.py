"""DNS driver registry.

Per CLAUDE.md non-negotiable #10, the service layer obtains a driver via
``get_driver(server_type)`` and speaks only to the abstract interface.

SpatiumDDI ships three authoritative DNS drivers: BIND9 (default), PowerDNS
(issue #127, Phase 1 — agent-managed alongside BIND9), and Windows DNS
(WinRM-driven, agentless). New drivers register here without touching the
service layer.
"""

from __future__ import annotations

from app.drivers.dns.azuredns import AzureDNSDriver
from app.drivers.dns.base import (
    ConfigBundle,
    DNSDriver,
    EffectiveBlocklistData,
    RecordChange,
    RecordChangeResult,
    RecordData,
    ServerOptions,
    ZoneData,
)
from app.drivers.dns.bind9 import BIND9Driver
from app.drivers.dns.cloudflare import CloudflareDNSDriver
from app.drivers.dns.digitalocean import DigitalOceanDNSDriver
from app.drivers.dns.googledns import GoogleCloudDNSDriver
from app.drivers.dns.hetzner import HetznerDNSDriver
from app.drivers.dns.linode import LinodeDNSDriver
from app.drivers.dns.powerdns import PowerDNSDriver
from app.drivers.dns.route53 import Route53DNSDriver
from app.drivers.dns.vultr import VultrDNSDriver
from app.drivers.dns.windows import WindowsDNSDriver

_DRIVERS: dict[str, type[DNSDriver]] = {
    "bind9": BIND9Driver,
    "powerdns": PowerDNSDriver,
    "windows_dns": WindowsDNSDriver,
    # Agentless cloud-hosted DNS providers (issue #37, Part B). The
    # control plane calls the provider REST/SDK API directly — same
    # shape as windows_dns Path B. Their modules lazy-import their heavy
    # SDKs inside methods, so importing them here is cheap + safe.
    "cloudflare": CloudflareDNSDriver,
    "route53": Route53DNSDriver,
    "azure_dns": AzureDNSDriver,
    "google_dns": GoogleCloudDNSDriver,
    # Token-only providers (issue #327) — single API token, plain JSON
    # over httpx, same agentless CloudDNSDriverBase shape.
    "digitalocean": DigitalOceanDNSDriver,
    "hetzner": HetznerDNSDriver,
    "linode": LinodeDNSDriver,
    "vultr": VultrDNSDriver,
}

# Drivers whose record ops run from the control plane directly, with no
# agent co-located with the daemon. The record_ops service short-circuits
# the queue for these — applying the change synchronously and writing a
# DNSRecordOp row as ``applied`` / ``failed`` for audit purposes.
AGENTLESS_DRIVERS: frozenset[str] = frozenset(
    {
        "windows_dns",
        "cloudflare",
        "route53",
        "azure_dns",
        "google_dns",
        "digitalocean",
        "hetzner",
        "linode",
        "vultr",
    }
)

# Agentless cloud-hosted DNS drivers (subset of AGENTLESS_DRIVERS).
# Used by the DNS server create/update path + the cloud import + the
# sync-from-server widening to recognise a cloud DNS server (credentials
# live in DNSServer.credentials_encrypted as a provider-specific dict).
CLOUD_DNS_DRIVERS: frozenset[str] = frozenset(
    {
        "cloudflare",
        "route53",
        "azure_dns",
        "google_dns",
        "digitalocean",
        "hetzner",
        "linode",
        "vultr",
    }
)


def is_agentless(driver_name: str) -> bool:
    """True if the driver runs from the control plane without an agent."""
    return driver_name in AGENTLESS_DRIVERS


def get_driver(server_type: str) -> DNSDriver:
    """Return a driver instance for the given server type string."""
    cls = _DRIVERS.get(server_type)
    if cls is None:
        raise ValueError(f"Unknown DNS driver: {server_type!r}")
    return cls()


def register_driver(name: str, driver_cls: type[DNSDriver]) -> None:
    """Register (or override) a driver class by name. Useful for tests."""
    _DRIVERS[name] = driver_cls


__all__ = [
    "AGENTLESS_DRIVERS",
    "CLOUD_DNS_DRIVERS",
    "ConfigBundle",
    "DNSDriver",
    "EffectiveBlocklistData",
    "RecordChange",
    "RecordChangeResult",
    "RecordData",
    "ServerOptions",
    "ZoneData",
    "get_driver",
    "register_driver",
]
