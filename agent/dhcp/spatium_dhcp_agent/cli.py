"""Alias module so ``spatium-dhcp-agent`` console-script works.

Re-exports :func:`spatium_dhcp_agent.__main__.main` as the entrypoint.
"""

from __future__ import annotations

from .__main__ import main

__all__ = ["main"]
