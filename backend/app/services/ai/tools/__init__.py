"""Operator Copilot tool registry — re-export the shared registry +
trigger tool registration on import.

Import this module from anywhere that needs to dispatch tools (the
chat orchestrator in Wave 3, the MCP endpoint in Wave 2). The
side-effect imports below register every read-only tool.
"""

# Side-effect imports — each module's @register_tool decorators run on
# import and populate ``REGISTRY``.
from app.services.ai.tools import (  # noqa: F401, E402
    dhcp,
    dns,
    ipam,
    network,
    network_modeling,
    nmap,
    ops,
    proposals,
    vendor,
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
