"""DHCP driver registry.

Per CLAUDE.md non-negotiable #10, the service layer obtains a driver via
``get_driver(server_type)`` and speaks only to the abstract interface.
"""

from __future__ import annotations

from app.drivers.dhcp.base import DHCPDriver
from app.drivers.dhcp.kea import KeaDriver

_DRIVERS: dict[str, type[DHCPDriver]] = {
    "kea": KeaDriver,
}


def get_driver(server_type: str) -> DHCPDriver:
    cls = _DRIVERS.get(server_type)
    if cls is None:
        raise ValueError(f"Unknown DHCP driver: {server_type!r}")
    return cls()


def register_driver(name: str, driver_cls: type[DHCPDriver]) -> None:
    _DRIVERS[name] = driver_cls


__all__ = ["get_driver", "register_driver"]
