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
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# ── Neutral record / scope data shapes ──────────────────────────────────────


# Canonical DHCP options SpatiumDDI models as first-class.
# Standard options use their RFC 2132 / IANA names; Kea names map 1:1.
STANDARD_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "routers",  # option 3
        "dns-servers",  # option 6  (domain-name-servers)
        "domain-name",  # option 15
        "broadcast-address",  # option 28
        "ntp-servers",  # option 42  (NTP servers — requested explicitly)
        "tftp-server-name",  # option 66
        "bootfile-name",  # option 67
        "tftp-server-address",  # option 150 (TFTP via address, Cisco IP phones)
        "domain-search",  # option 119
        "mtu",  # option 26
        "time-offset",  # option 2
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
class PXEClassDef:
    """A rendered PXE / iPXE client class (issue #51).

    Distinct shape from :class:`ClientClassDef` because PXE classes
    carry per-class ``next_server`` + ``boot_file_name`` rather than
    relying on Kea's global option-data — the boot file the client
    pulls is what differentiates BIOS / UEFI / HTTP-boot
    architectures, so each match must override these fields. Kea
    evaluates classes in declared order; we render in (priority ASC,
    profile_id ASC, match_id ASC) so config diffs stay stable.

    ``match_expression`` is the Kea ``test`` expression composed
    from the operator's ``vendor_class_match`` (option 60 substring)
    and ``arch_codes`` (option 93 enumeration) on
    ``DHCPPXEArchMatch``. Empty match = always match (paired with a
    low priority so it acts as a fallthrough).
    """

    name: str
    match_expression: str
    next_server: str
    boot_file_name: str
    is_ipxe_chain: bool = False


@dataclass(frozen=True)
class MACBlockDef:
    """A blocked MAC address — group-global deny entry.

    Rendered into Kea's reserved ``DROP`` client class and into the
    Windows DHCP server-level deny-filter list. ``mac_address`` is
    normalized to colon-separated lowercase (``aa:bb:cc:dd:ee:ff``)
    before it reaches the agent.
    """

    mac_address: str
    reason: str = "other"
    description: str = ""


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
    # "ipv4" → Dhcp4 rendering, "ipv6" → Dhcp6 rendering. Defaults to "ipv4"
    # so legacy bundles (before address_family existed) keep working.
    address_family: str = "ipv4"


@dataclass(frozen=True)
class ServerOptionsDef:
    """Global server options (inherited by scopes unless overridden)."""

    options: dict[str, Any] = field(default_factory=dict)
    lease_time: int = 86400


@dataclass(frozen=True)
class FailoverConfig:
    """Kea HA (``libdhcp_ha.so``) configuration for one peer.

    Carried on ``ConfigBundle.failover`` when the server is a member of
    a ``DHCPServerGroup`` that contains another Kea peer. HA tuning
    comes from the group's columns; per-peer URLs come from each
    ``DHCPServer.ha_peer_url``. ``this_server_name`` tells the local
    peer which of the two entries in ``peers`` refers to itself — Kea
    keys HA rules off the matching ``name``.

    ``channel_id`` / ``channel_name`` retain their names for wire
    compatibility with the agent, but under the group-centric model
    they carry the group's id / name.
    """

    channel_id: str
    channel_name: str
    mode: str  # load-balancing | hot-standby
    this_server_name: str  # name used in the local peer's "this-server-name"
    peers: tuple[dict[str, Any], ...]  # [{name, url, role, auto-failover}, ...]
    heartbeat_delay_ms: int = 10000
    max_response_delay_ms: int = 60000
    max_ack_delay_ms: int = 10000
    max_unacked_clients: int = 5


@dataclass
class ConfigBundle:
    """Everything an agent needs to configure and run a DHCP daemon.

    Hash-keyed (``etag``) so the agent long-poll endpoint can return
    ``304 Not Modified`` when nothing has changed.
    """

    server_id: str
    server_name: str
    driver: str  # kea | windows_dhcp
    roles: tuple[str, ...]
    options: ServerOptionsDef
    scopes: tuple[ScopeDef, ...]
    client_classes: tuple[ClientClassDef, ...]
    generated_at: datetime
    mac_blocks: tuple[MACBlockDef, ...] = ()
    pxe_classes: tuple[PXEClassDef, ...] = ()
    etag: str = ""
    # Populated when the server's group has ≥ 2 Kea members. The
    # agent's Kea renderer injects ``libdhcp_ha.so`` + the ``high-
    # availability`` config block when this is present.
    failover: FailoverConfig | None = None

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
            "mac_blocks": [asdict(m) for m in self.mac_blocks],
            "pxe_classes": [asdict(p) for p in self.pxe_classes],
            "failover": asdict(self.failover) if self.failover else None,
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()


# ── Batch write items / results ─────────────────────────────────────────────
#
# These mirror the DNS side (``RecordChange`` / ``RecordChangeResult``) —
# neutral input + output shapes so the service layer can dispatch many
# ops as one call to the driver and the driver decides whether to ship
# them one at a time (ABC default) or as a single chunked WinRM round
# trip (Windows DHCP override).


@dataclass(frozen=True)
class ReservationItem:
    """One reservation to upsert. Matches ``apply_reservation`` args."""

    scope_id: str
    ip_address: str
    mac_address: str
    hostname: str = ""
    description: str = ""


@dataclass(frozen=True)
class RemoveReservationItem:
    """One reservation to delete by (scope_id, mac_address)."""

    scope_id: str
    mac_address: str


@dataclass(frozen=True)
class ExclusionItem:
    """One exclusion range to add."""

    scope_id: str
    start_ip: str
    end_ip: str


@dataclass(frozen=True)
class ReservationResult:
    """Per-op result from ``apply_reservations`` / ``remove_reservations``.

    ``item`` is the input echoed back so callers can zip results with
    their source list without re-tracking identity. Per-op failures
    surface as ``ok=False`` with ``error`` populated; whole-batch
    failures raise from the driver (auth, connection refused, PS
    parse error) and never land here.
    """

    ok: bool
    item: ReservationItem | RemoveReservationItem
    error: str | None = None


@dataclass(frozen=True)
class ExclusionResult:
    """Per-op result from ``apply_exclusions``."""

    ok: bool
    item: ExclusionItem
    error: str | None = None


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

    # ── Optional per-object write APIs (Windows DHCP today) ────────────
    #
    # Agent-based drivers (Kea, ISC) configure the daemon through a
    # full-bundle ``apply_config`` — per-object CRUD doesn't apply. Only
    # the Windows driver overrides these. The default implementations
    # raise ``NotImplementedError`` so a caller misrouting a write
    # against a non-Windows server surfaces a clear error instead of
    # silently no-oping.
    #
    # Batch counterparts (``apply_reservations`` etc.) default to a
    # sequential loop over the singular method — so any driver that
    # implements the singular method automatically inherits correct (if
    # slow) bulk behaviour. Windows overrides these to ship each chunk
    # in one WinRM round trip.

    async def apply_reservations(
        self, server: Any, *, items: Sequence[ReservationItem]
    ) -> list[ReservationResult]:
        """Upsert many DHCP reservations against ``server``.

        Default: sequential loop over ``apply_reservation``. Windows DHCP
        overrides to use a single chunked PowerShell script per WinRM
        round trip.
        """
        results: list[ReservationResult] = []
        for item in items:
            try:
                await self.apply_reservation(  # type: ignore[attr-defined]
                    server,
                    scope_id=item.scope_id,
                    ip_address=item.ip_address,
                    mac_address=item.mac_address,
                    hostname=item.hostname,
                    description=item.description,
                )
                results.append(ReservationResult(ok=True, item=item))
            except Exception as exc:  # noqa: BLE001 — per-op isolation
                results.append(ReservationResult(ok=False, item=item, error=str(exc)))
        return results

    async def remove_reservations(
        self, server: Any, *, items: Sequence[RemoveReservationItem]
    ) -> list[ReservationResult]:
        """Delete many DHCP reservations by (scope_id, mac_address)."""
        results: list[ReservationResult] = []
        for item in items:
            try:
                await self.remove_reservation(  # type: ignore[attr-defined]
                    server, scope_id=item.scope_id, mac_address=item.mac_address
                )
                results.append(ReservationResult(ok=True, item=item))
            except Exception as exc:  # noqa: BLE001 — per-op isolation
                results.append(ReservationResult(ok=False, item=item, error=str(exc)))
        return results

    async def apply_exclusions(
        self, server: Any, *, items: Sequence[ExclusionItem]
    ) -> list[ExclusionResult]:
        """Add many exclusion ranges."""
        results: list[ExclusionResult] = []
        for item in items:
            try:
                await self.apply_exclusion(  # type: ignore[attr-defined]
                    server,
                    scope_id=item.scope_id,
                    start_ip=item.start_ip,
                    end_ip=item.end_ip,
                )
                results.append(ExclusionResult(ok=True, item=item))
            except Exception as exc:  # noqa: BLE001 — per-op isolation
                results.append(ExclusionResult(ok=False, item=item, error=str(exc)))
        return results

    async def sync_mac_blocks(
        self, server: Any, *, desired: Sequence[MACBlockDef]
    ) -> tuple[int, int]:
        """Reconcile the server's MAC deny-list against ``desired``.

        Windows DHCP has a server-level deny filter list that we keep in
        sync with the SpatiumDDI group's ``DHCPMACBlock`` rows. Kea-based
        drivers do NOT implement this: their blocklist ships via the
        ConfigBundle + rendered DROP class, not as a per-object write.

        Returns ``(added, removed)``. Drivers that don't need to push
        per-object MAC filters (everything except Windows) inherit this
        default no-op — the bundle path covers them.
        """
        return (0, 0)


__all__ = [
    "STANDARD_OPTION_NAMES",
    "ClientClassDef",
    "ConfigBundle",
    "DHCPDriver",
    "ExclusionItem",
    "ExclusionResult",
    "MACBlockDef",
    "PXEClassDef",
    "PoolDef",
    "RemoveReservationItem",
    "ReservationItem",
    "ReservationResult",
    "ScopeDef",
    "ServerOptionsDef",
    "StaticAssignmentDef",
]
