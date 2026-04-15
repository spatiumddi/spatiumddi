"""Abstract DHCP driver base class and neutral data structures.

Mirrors ``app.drivers.dns.base``. The control-plane DHCP driver is a thin
translator: it renders DB state into a canonical backend-neutral
``ConfigBundle``, hash-keyed by SHA-256 ETag for agent long-poll. Per
CLAUDE.md non-negotiable #10, no daemon specifics leak into the service
layer — the service only touches ``DHCPDriver``.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


# ── Neutral record / scope data shapes ──────────────────────────────────────


# Canonical DHCP options SpatiumDDI models as first-class.
# Standard options use their RFC 2132 / IANA names; Kea names map 1:1.
STANDARD_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "routers",              # option 3
        "dns-servers",          # option 6  (domain-name-servers)
        "domain-name",          # option 15
        "broadcast-address",    # option 28
        "ntp-servers",          # option 42  (NTP servers — requested explicitly)
        "tftp-server-name",     # option 66
        "bootfile-name",        # option 67
        "tftp-server-address",  # option 150 (TFTP via address, Cisco IP phones)
        "domain-search",        # option 119
        "mtu",                  # option 26
        "time-offset",          # option 2
    }
)


@dataclass(frozen=True)
class PoolDef:
    """A pool (range) inside a scope."""

    start_ip: str
    end_ip: str
    pool_type: str = "dynamic"  # dynamic | excluded | reserved
    name: str = ""
    class_restriction: str | None = None
    lease_time_override: int | None = None
    options_override: dict[str, Any] | None = None


@dataclass(frozen=True)
class StaticAssignmentDef:
    """A reservation (MAC/client-id → IP) inside a scope."""

    ip_address: str
    mac_address: str
    hostname: str = ""
    client_id: str | None = None
    options_override: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClientClassDef:
    """A client class with a match expression and option overrides."""

    name: str
    match_expression: str = ""
    description: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScopeDef:
    """A DHCP scope — one subnet configuration."""

    subnet_cidr: str
    lease_time: int = 86400
    min_lease_time: int | None = None
    max_lease_time: int | None = None
    options: dict[str, Any] = field(default_factory=dict)
    pools: tuple[PoolDef, ...] = ()
    statics: tuple[StaticAssignmentDef, ...] = ()
    ddns_enabled: bool = False
    ddns_hostname_policy: str = "client"
    is_active: bool = True


@dataclass(frozen=True)
class ServerOptionsDef:
    """Global server options (inherited by scopes unless overridden)."""

    options: dict[str, Any] = field(default_factory=dict)
    lease_time: int = 86400


@dataclass
class ConfigBundle:
    """Everything an agent needs to configure and run a DHCP daemon.

    Hash-keyed (``etag``) so the agent long-poll endpoint can return
    ``304 Not Modified`` when nothing has changed.
    """

    server_id: str
    server_name: str
    driver: str  # kea | isc_dhcp
    roles: tuple[str, ...]
    options: ServerOptionsDef
    scopes: tuple[ScopeDef, ...]
    client_classes: tuple[ClientClassDef, ...]
    generated_at: datetime
    etag: str = ""

    def compute_etag(self) -> str:
        """Compute a stable SHA-256 of the bundle contents (excluding etag/timestamp)."""
        payload = {
            "server_id": self.server_id,
            "server_name": self.server_name,
            "driver": self.driver,
            "roles": sorted(self.roles),
            "options": asdict(self.options),
            "scopes": [asdict(s) for s in self.scopes],
            "client_classes": [asdict(c) for c in self.client_classes],
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()


# ── Driver abstract base ────────────────────────────────────────────────────


class DHCPDriver(ABC):
    """Abstract base class for DHCP backend drivers.

    Drivers are pure renderers + single-op appliers. Daemon lifecycle runs in
    the agent container; the control plane only formulates config. Stateless.
    """

    name: str = "abstract"

    @abstractmethod
    def render_config(self, bundle: ConfigBundle) -> str:
        """Render the daemon's top-level config (JSON for Kea, text for ISC)."""

    @abstractmethod
    async def apply_config(self, server: Any, bundle: ConfigBundle) -> None:
        """Push and activate a full config bundle (agent-side)."""

    @abstractmethod
    async def reload(self, server: Any) -> None:
        """Instruct the daemon to re-read its config."""

    @abstractmethod
    async def restart(self, server: Any) -> None:
        """Restart the daemon (used on unrecoverable config changes)."""

    @abstractmethod
    async def get_leases(self, server: Any) -> list[dict[str, Any]]:
        """Fetch the current lease list from the daemon."""

    @abstractmethod
    async def health_check(self, server: Any) -> tuple[bool, str]:
        """Return (ok, message)."""

    @abstractmethod
    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        """Validate a bundle before apply. Returns (ok, errors)."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Return a dict describing what this driver supports."""


__all__ = [
    "STANDARD_OPTION_NAMES",
    "ClientClassDef",
    "ConfigBundle",
    "DHCPDriver",
    "PoolDef",
    "ScopeDef",
    "ServerOptionsDef",
    "StaticAssignmentDef",
]
