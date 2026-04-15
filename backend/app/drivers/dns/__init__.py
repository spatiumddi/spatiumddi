"""DNS driver registry.

Per CLAUDE.md non-negotiable #10, the service layer obtains a driver via
``get_driver(server_type)`` and speaks only to the abstract interface.
"""

from __future__ import annotations

from typing import Any

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


class _PowerDNSStub(DNSDriver):
    """Stub — PowerDNS driver lands in Wave 2.x."""

    name = "powerdns"

    def _nope(self) -> None:
        raise NotImplementedError("PowerDNS driver not yet implemented")

    def render_server_config(self, server: Any, options: ServerOptions, *, bundle: ConfigBundle | None = None) -> str:  # noqa: D401,E501
        self._nope()
        return ""

    def render_zone_config(self, zone: ZoneData) -> str:
        self._nope()
        return ""

    def render_zone_file(self, zone: ZoneData, records: list[RecordData]) -> str:
        self._nope()
        return ""

    def render_rpz_zone(self, blocklist: EffectiveBlocklistData) -> str:
        self._nope()
        return ""

    async def apply_record_change(self, server: Any, change: RecordChange) -> None:
        self._nope()

    async def reload_config(self, server: Any) -> None:
        self._nope()

    async def reload_zone(self, server: Any, zone_name: str) -> None:
        self._nope()

    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        self._nope()
        return (False, [])

    def capabilities(self) -> dict[str, Any]:
        return {"name": "powerdns", "implemented": False}


_DRIVERS: dict[str, type[DNSDriver]] = {
    "bind9": BIND9Driver,
    "powerdns": _PowerDNSStub,
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


__all__ = ["DNSDriver", "get_driver", "register_driver"]
