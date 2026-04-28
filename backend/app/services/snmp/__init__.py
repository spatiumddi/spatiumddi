"""SNMP-based network discovery service layer.

The poller wraps pysnmp's async HLAPI and walks standard MIBs only;
the cross-reference helper feeds ARP results back into IPAM. The
Celery task ``app.tasks.snmp_poll.poll_device`` is the orchestrator
that ties the two together with persistence.
"""

from __future__ import annotations

from .cross_reference import cross_reference_arp
from .errors import (
    SNMPAuthError,
    SNMPError,
    SNMPProtocolError,
    SNMPTimeoutError,
    SNMPTransportError,
)
from .poller import (
    ArpData,
    FdbData,
    InterfaceData,
    SysInfo,
    test_connection,
    walk_arp,
    walk_fdb,
    walk_interfaces,
)

__all__ = [
    "ArpData",
    "FdbData",
    "InterfaceData",
    "SysInfo",
    "SNMPError",
    "SNMPAuthError",
    "SNMPProtocolError",
    "SNMPTimeoutError",
    "SNMPTransportError",
    "cross_reference_arp",
    "test_connection",
    "walk_arp",
    "walk_fdb",
    "walk_interfaces",
]
