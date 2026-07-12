"""DHCP driver registry.

Per CLAUDE.md non-negotiable #10, the service layer obtains a driver via
``get_driver(server_type)`` and speaks only to the abstract interface.

Mirrors ``app.drivers.dns`` for the ``AGENTLESS_DRIVERS`` concept: drivers
whose calls run from the control plane directly (no co-located agent).
``windows_dhcp`` (WinRM) and ``fortigate`` (FortiOS REST) are agentless;
the scheduled lease-pull task and sync-leases-now endpoint call the driver
directly instead of enqueueing a config op. ``windows_dhcp`` is read-only
for the bundle-push flow (its writes ride per-object write-through);
``fortigate`` supports a synchronous full reconcile on ``/sync``.
"""

from __future__ import annotations

from app.drivers.dhcp.base import DHCPDriver
from app.drivers.dhcp.fortigate import FortiGateDHCPDriver
from app.drivers.dhcp.kea import KeaDriver
from app.drivers.dhcp.windows import WindowsDHCPReadOnlyDriver

_DRIVERS: dict[str, type[DHCPDriver]] = {
    "kea": KeaDriver,
    "windows_dhcp": WindowsDHCPReadOnlyDriver,
    "fortigate": FortiGateDHCPDriver,
}

# Drivers that run from the control plane without a co-located agent.
AGENTLESS_DRIVERS: frozenset[str] = frozenset({"windows_dhcp", "fortigate"})

# Drivers that only support reads (lease monitoring) — never participate
# in config-push flows. UI hides the "Sync / Push config" actions for
# these and substitutes the read-only lease-sync actions instead.
# ``fortigate`` is agentless but NOT read-only: it supports a full
# synchronous reconcile on /sync.
READ_ONLY_DRIVERS: frozenset[str] = frozenset({"windows_dhcp"})

# Agentless drivers driven over a cloud/REST API (subclass
# ``AgentlessDHCPDriverBase``) — the write-through re-pushes the whole
# scope object on every edit. Distinct from ``windows_dhcp`` (per-object
# WinRM cmdlets).
CLOUD_DHCP_DRIVERS: frozenset[str] = frozenset({"fortigate"})


def get_driver(server_type: str) -> DHCPDriver:
    cls = _DRIVERS.get(server_type)
    if cls is None:
        raise ValueError(f"Unknown DHCP driver: {server_type!r}")
    return cls()


def register_driver(name: str, driver_cls: type[DHCPDriver]) -> None:
    _DRIVERS[name] = driver_cls


def is_agentless(driver_name: str) -> bool:
    return driver_name in AGENTLESS_DRIVERS


def is_read_only(driver_name: str) -> bool:
    return driver_name in READ_ONLY_DRIVERS


def is_cloud(driver_name: str) -> bool:
    return driver_name in CLOUD_DHCP_DRIVERS


__all__ = [
    "AGENTLESS_DRIVERS",
    "CLOUD_DHCP_DRIVERS",
    "READ_ONLY_DRIVERS",
    "get_driver",
    "is_agentless",
    "is_cloud",
    "is_read_only",
    "register_driver",
]
