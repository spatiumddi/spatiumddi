"""Agent-side driver base class.

Mirrors the control-plane DNSDriverBase but on the container side — the agent
asks its driver to render configs, reload the daemon, and apply RFC 2136 /
RFC 2136 record ops over loopback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class DriverBase(ABC):
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    @abstractmethod
    def render(self, bundle: dict[str, Any]) -> None:
        """Render config files (atomic to ``rendered.new``)."""

    @abstractmethod
    def validate(self) -> None:
        """Validate the rendered config. Raise on failure."""

    @abstractmethod
    def swap_and_reload(self) -> None:
        """Rename rendered.new → rendered, signal the daemon to reload."""

    @abstractmethod
    def apply_record_op(self, op: dict[str, Any]) -> None:
        """Apply a RecordOp via loopback nsupdate via RFC 2136."""

    @abstractmethod
    def start_daemon(self) -> None:
        """Spawn the DNS daemon. Called once at startup."""

    @abstractmethod
    def daemon_running(self) -> bool:
        ...

    def apply_config(self, bundle: dict[str, Any]) -> None:
        """Default orchestration: render → validate → swap+reload."""
        self.render(bundle)
        self.validate()
        self.swap_and_reload()
