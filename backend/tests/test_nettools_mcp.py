"""Tests for the network-tools MCP tools (#58).

Assert the seven tools are registered with the right module tag +
default-enabled state, and that disabling ``tools.network`` strips them
from the effective set via ``effective_tool_names``.
"""

from __future__ import annotations

import pytest

from app.services.ai.tools import REGISTRY  # noqa: F401 — triggers registration
from app.services.ai.tools.base import effective_tool_names
from app.services.ai.tools.nettools import NetworkDigArgs
from app.services.feature_modules import all_module_ids

_NETTOOL_NAMES = [
    "network_ping",
    "network_traceroute",
    "network_dig",
    "network_port_test",
    "network_tls_cert",
    "lookup_mac_vendor",
    "network_whois",
]


def test_seven_tools_registered_with_module_tag() -> None:
    for name in _NETTOOL_NAMES:
        tool = REGISTRY.get(name)
        assert tool is not None, f"{name} not registered"
        assert tool.module == "tools.network", name
        assert tool.category == "tools", name
        assert tool.writes is False, name


def test_whois_default_disabled_others_enabled() -> None:
    whois = REGISTRY.get("network_whois")
    assert whois is not None and whois.default_enabled is False
    for name in _NETTOOL_NAMES:
        if name == "network_whois":
            continue
        tool = REGISTRY.get(name)
        assert tool is not None and tool.default_enabled is True, name


def test_module_is_in_catalog() -> None:
    assert "tools.network" in all_module_ids()


def test_module_disabled_strips_tools() -> None:
    # All modules enabled EXCEPT tools.network.
    enabled = all_module_ids() - {"tools.network"}
    eff = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=enabled,
    )
    for name in _NETTOOL_NAMES:
        assert name not in eff, f"{name} should be stripped when tools.network is off"


def test_module_enabled_keeps_default_tools() -> None:
    enabled = all_module_ids()  # everything on
    eff = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=enabled,
    )
    # default-enabled tools present; whois (default-off) absent from the
    # registry-default set.
    assert "network_ping" in eff
    assert "lookup_mac_vendor" in eff
    assert "network_whois" not in eff


# ── MCP dig args validation (mirrors the REST DigRequest schema) ────


def test_network_dig_args_rejects_leading_dash_name() -> None:
    # The MCP path historically had no ``name`` validator, so ``-froot``
    # flowed into run_dig → build_dig_argv. dig has no ``--`` terminator,
    # so a leading-dash name is parsed as a flag (``-f`` batch-mode reads
    # queries from a filesystem path). The validator now rejects it.
    with pytest.raises(ValueError):
        NetworkDigArgs(name="-froot", record_type="A")
    with pytest.raises(ValueError):
        NetworkDigArgs(name="bad name with spaces", record_type="A")
    # valid names still pass
    assert NetworkDigArgs(name="example.com", record_type="A").name == "example.com"
    assert NetworkDigArgs(name="_dmarc.example.com").name == "_dmarc.example.com"


def test_network_dig_args_rejects_blocked_server() -> None:
    # @server is SSRF-guarded via the shared denylist.
    with pytest.raises(ValueError):
        NetworkDigArgs(name="example.com", server="169.254.169.254")
    with pytest.raises(ValueError):
        NetworkDigArgs(name="example.com", server="127.0.0.1")
    # public + RFC1918 resolvers are allowed
    assert NetworkDigArgs(name="example.com", server="8.8.8.8").server == "8.8.8.8"
    assert NetworkDigArgs(name="example.com", server="10.0.0.53").server == "10.0.0.53"
