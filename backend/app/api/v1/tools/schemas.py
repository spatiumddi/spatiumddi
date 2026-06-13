"""Re-export of the network-tools Pydantic models (#58).

The actual definitions live in :mod:`app.services.nettools.schemas` so
the service-layer runner / socket modules can import the result models +
``validate_host`` without a circular import back through this api
package. Handlers import from here for locality.
"""

from __future__ import annotations

from app.services.nettools.schemas import (
    CommandResult,
    DigRequest,
    FirewallLogLine,
    FirewallLogsRequest,
    FirewallLogsResult,
    HostRequest,
    MacVendorEntry,
    MacVendorRequest,
    MacVendorResult,
    NetToolTarget,
    PortTestRequest,
    PortTestResult,
    PropagationRequest,
    TlsCertRequest,
    TlsCertResult,
    WhoisRequest,
    validate_host,
    validate_host_or_cidr,
)

__all__ = [
    "CommandResult",
    "DigRequest",
    "FirewallLogLine",
    "FirewallLogsRequest",
    "FirewallLogsResult",
    "HostRequest",
    "MacVendorEntry",
    "MacVendorRequest",
    "MacVendorResult",
    "NetToolTarget",
    "PortTestRequest",
    "PortTestResult",
    "PropagationRequest",
    "TlsCertRequest",
    "TlsCertResult",
    "WhoisRequest",
    "validate_host",
    "validate_host_or_cidr",
]
