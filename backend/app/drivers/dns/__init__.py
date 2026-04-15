"""DNS driver registry.

Per CLAUDE.md non-negotiable #10, the service layer obtains a driver via
``get_driver(server_type)`` and speaks only to the abstract interface.

SpatiumDDI ships with BIND9 as the single supported backend. The registry
pattern is kept so alternative drivers (e.g. a hidden-primary over AXFR
setup for a specific deployment) can be registered without touching the
service layer.
"""

from __future__ import annotations

from app.drivers.dns.base import (
    ConfigBundle,
    DNSDriver,
    EffectiveBlocklistData,
    RecordChange,
    RecordData,
    ServerOptions,
    ZoneData,
)
from app.drivers.dns.bind9 import BIND9Driver

_DRIVERS: dict[str, type[DNSDriver]] = {
    "bind9": BIND9Driver,
}


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
    "ConfigBundle",
    "DNSDriver",
    "EffectiveBlocklistData",
    "RecordChange",
    "RecordData",
    "ServerOptions",
    "ZoneData",
    "get_driver",
    "register_driver",
]
