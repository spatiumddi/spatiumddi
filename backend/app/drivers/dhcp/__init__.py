"""DHCP driver package.

Per CLAUDE.md non-negotiable #10, the service layer obtains a driver via
``get_driver(server_type)`` and speaks only to the abstract interface.
"""

from app.drivers.dhcp.base import (
    ClientClassDef,
    ConfigBundle,
    DHCPDriver,
    ExclusionItem,
    ExclusionResult,
    PoolDef,
    RemoveReservationItem,
    ReservationItem,
    ReservationResult,
    ScopeDef,
    ServerOptionsDef,
    StaticAssignmentDef,
)
from app.drivers.dhcp.registry import (
    AGENTLESS_DRIVERS,
    READ_ONLY_DRIVERS,
    get_driver,
    is_agentless,
    is_read_only,
    register_driver,
)

__all__ = [
    "AGENTLESS_DRIVERS",
    "READ_ONLY_DRIVERS",
    "ClientClassDef",
    "ConfigBundle",
    "DHCPDriver",
    "ExclusionItem",
    "ExclusionResult",
    "PoolDef",
    "RemoveReservationItem",
    "ReservationItem",
    "ReservationResult",
    "ScopeDef",
    "ServerOptionsDef",
    "StaticAssignmentDef",
    "get_driver",
    "is_agentless",
    "is_read_only",
    "register_driver",
]
