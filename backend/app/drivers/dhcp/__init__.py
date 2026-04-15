"""DHCP driver package.

Per CLAUDE.md non-negotiable #10, the service layer obtains a driver via
``get_driver(server_type)`` and speaks only to the abstract interface.
"""

from app.drivers.dhcp.base import (
    ClientClassDef,
    ConfigBundle,
    DHCPDriver,
    PoolDef,
    ScopeDef,
    ServerOptionsDef,
    StaticAssignmentDef,
)
from app.drivers.dhcp.registry import get_driver, register_driver

__all__ = [
    "ClientClassDef",
    "ConfigBundle",
    "DHCPDriver",
    "PoolDef",
    "ScopeDef",
    "ServerOptionsDef",
    "StaticAssignmentDef",
    "get_driver",
    "register_driver",
]
