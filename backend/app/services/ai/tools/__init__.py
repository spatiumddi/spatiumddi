"""Operator Copilot tool registry — re-export the shared registry +
trigger tool registration on import.

Import this module from anywhere that needs to dispatch tools (the
chat orchestrator in Wave 3, the MCP endpoint in Wave 2). The
side-effect imports below register every read-only tool.
"""

# Side-effect imports — each module's @register_tool decorators run on
# import and populate ``REGISTRY``.
from app.services.ai.tools import (  # noqa: F401, E402
    admin,
    appliance,
    apt,
    auth_grants,
    backup,
    bgp,
    certificates,
    changes,
    conformity,
    copilot,
    dhcp,
    diagnostics,
    dns,
    firewall,
    imports,
    integrations,
    ipam,
    lldp,
    maintenance,
    multicast,
    nettools,
    network,
    network_modeling,
    nmap,
    ntp,
    observability,
    ops,
    ownership,
    pairing,
    pcap,
    proposals,
    redis,
    reports,
    resolver,
    saved_views,
    snmp,
    ssh,
    syslog,
    tls_certs,
    upgrades,
    vendor,
    webhooks,
    whois,
)
from app.services.ai.tools.base import (
    REGISTRY,
    Tool,
    ToolArgumentError,
    ToolDisabled,
    ToolNotFound,
    ToolRegistry,
    effective_tool_names,
    register_tool,
)

__all__ = [
    "REGISTRY",
    "Tool",
    "ToolRegistry",
    "ToolNotFound",
    "ToolArgumentError",
    "ToolDisabled",
    "register_tool",
    "effective_tool_names",
]
