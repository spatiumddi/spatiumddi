"""Abstract DNS driver base class and neutral data structures.

The control-plane DNS driver is a *thin* translator: it takes SpatiumDDI DB
models and emits a canonical, backend-neutral `ConfigBundle` (plus per-record
`RecordChange` ops). The actual daemon lifecycle (nsupdate, rndc) runs
inside the agent container — see ``docs/deployment/DNS_AGENT.md`` §3.

CLAUDE.md non-negotiable #10: no daemon specifics leak into the service
layer. The service layer calls ``get_driver(server.driver)`` and receives a
``DNSDriver`` instance whose public methods speak only in neutral types.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


# ── Neutral record / zone data shapes ──────────────────────────────────────


@dataclass(frozen=True)
class RecordData:
    """A single DNS resource record, relative to its zone."""

    name: str              # relative label ("@" = apex)
    record_type: str       # A | AAAA | CNAME | MX | TXT | NS | PTR | SRV | CAA | ...
    value: str
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None


@dataclass(frozen=True)
class ZoneData:
    name: str              # FQDN with trailing dot, e.g. "example.com."
    zone_type: str         # primary | secondary | stub | forward
    kind: str              # forward | reverse
    ttl: int
    refresh: int
    retry: int
    expire: int
    minimum: int
    primary_ns: str
    admin_email: str
    serial: int
    records: tuple[RecordData, ...] = ()
    allow_query: tuple[str, ...] | None = None
    allow_transfer: tuple[str, ...] | None = None
    also_notify: tuple[str, ...] | None = None
    notify_enabled: str | None = None
    view_name: str | None = None


@dataclass(frozen=True)
class ViewData:
    name: str
    match_clients: tuple[str, ...]
    match_destinations: tuple[str, ...]
    recursion: bool
    order: int


@dataclass(frozen=True)
class AclData:
    name: str
    # (value, negate) tuples, in order
    entries: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class TrustAnchorData:
    zone_name: str
    algorithm: int
    key_tag: int
    public_key: str
    is_initial_key: bool


@dataclass(frozen=True)
class ServerOptions:
    forwarders: tuple[str, ...] = ()
    forward_policy: str = "first"
    recursion_enabled: bool = True
    allow_recursion: tuple[str, ...] = ("any",)
    dnssec_validation: str = "auto"
    notify_enabled: str = "yes"
    also_notify: tuple[str, ...] = ()
    allow_notify: tuple[str, ...] = ()
    allow_query: tuple[str, ...] = ("any",)
    allow_query_cache: tuple[str, ...] = ("localhost", "localnets")
    allow_transfer: tuple[str, ...] = ("none",)
    blackhole: tuple[str, ...] = ()
    trust_anchors: tuple[TrustAnchorData, ...] = ()
    query_log_enabled: bool = False
    query_log_channel: str = "file"
    query_log_file: str = "/var/log/named/queries.log"
    query_log_severity: str = "info"
    query_log_print_category: bool = True
    query_log_print_severity: bool = True
    query_log_print_time: bool = True


@dataclass(frozen=True)
class TsigKey:
    name: str
    algorithm: str    # e.g. "hmac-sha256"
    secret: str       # base64


@dataclass(frozen=True)
class BlocklistEntry:
    domain: str
    action: str        # block | redirect | nxdomain
    block_mode: str    # nxdomain | sinkhole | refused
    sinkhole_ip: str | None
    target: str | None
    is_wildcard: bool


@dataclass(frozen=True)
class EffectiveBlocklistData:
    """Neutral projection of ``app.services.dns_blocklist.EffectiveBlocklist``.

    Drivers consume this to render backend-specific output (BIND9 RPZ
    Lua, etc.). The service-layer builder converts the service dataclass into
    this driver-neutral dataclass so drivers never import service modules.
    """

    rpz_zone_name: str               # e.g. "spatium-blocklist.rpz."
    entries: tuple[BlocklistEntry, ...]
    exceptions: frozenset[str]


@dataclass
class ConfigBundle:
    """Everything an agent needs to render and run a DNS daemon.

    The bundle is hash-keyed (``etag``) so the agent long-poll endpoint can
    return ``304 Not Modified`` when nothing has changed.
    """

    server_id: str
    server_name: str
    driver: str                       # bind9
    roles: tuple[str, ...]
    options: ServerOptions
    acls: tuple[AclData, ...]
    views: tuple[ViewData, ...]
    zones: tuple[ZoneData, ...]
    tsig_keys: tuple[TsigKey, ...]
    blocklists: tuple[EffectiveBlocklistData, ...]
    generated_at: datetime
    etag: str = ""

    def compute_etag(self) -> str:
        """Compute a stable SHA-256 of the bundle contents (excluding the etag/timestamp)."""
        payload = {
            "server_id": self.server_id,
            "server_name": self.server_name,
            "driver": self.driver,
            "roles": sorted(self.roles),
            "options": asdict(self.options),
            "acls": [asdict(a) for a in self.acls],
            "views": [asdict(v) for v in self.views],
            "zones": [asdict(z) for z in self.zones],
            "tsig_keys": [{"name": k.name, "algorithm": k.algorithm} for k in self.tsig_keys],
            "blocklists": [
                {
                    "rpz_zone_name": b.rpz_zone_name,
                    "entries": [asdict(e) for e in b.entries],
                    "exceptions": sorted(b.exceptions),
                }
                for b in self.blocklists
            ],
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()


# ── Per-record incremental ops (agent loopback nsupdate over TSIG) ─────────


@dataclass(frozen=True)
class RecordChange:
    """A single record mutation to be applied incrementally.

    The agent interprets ``op`` and ``record`` against its local daemon.
    """

    op: Literal["create", "update", "delete"]
    zone_name: str
    record: RecordData
    target_serial: int
    tsig_key_name: str | None = None
    op_id: str = ""                    # caller-supplied UUID for ACK tracking


# ── Driver abstract base ──────────────────────────────────────────────────


class DNSDriver(ABC):
    """Abstract base class for DNS backend drivers.

    Drivers are pure renderers + single-record appliers. They do not manage
    daemon lifecycle (the agent does that). They must be stateless and safe
    to instantiate per call.
    """

    name: str = "abstract"

    # ── Rendering ─────────────────────────────────────────────────────────

    @abstractmethod
    def render_server_config(
        self, server: Any, options: ServerOptions, *, bundle: ConfigBundle | None = None
    ) -> str:
        """Render the daemon's top-level config (e.g. ``named.conf``)."""

    @abstractmethod
    def render_zone_config(self, zone: ZoneData) -> str:
        """Render the per-zone stanza to be included in the server config."""

    @abstractmethod
    def render_zone_file(self, zone: ZoneData, records: list[RecordData]) -> str:
        """Render an RFC 1035-format zone file."""

    @abstractmethod
    def render_rpz_zone(self, blocklist: EffectiveBlocklistData) -> str:
        """Render an RPZ zone file (or equivalent) from an effective blocklist."""

    # ── Runtime (agent-side; control plane only *formulates* these) ──────

    @abstractmethod
    async def apply_record_change(self, server: Any, change: RecordChange) -> None:
        """Apply a single record change to the daemon (loopback RFC 2136 / API)."""

    @abstractmethod
    async def reload_config(self, server: Any) -> None:
        """Instruct the daemon to re-read its full config (e.g. ``rndc reconfig``)."""

    @abstractmethod
    async def reload_zone(self, server: Any, zone_name: str) -> None:
        """Instruct the daemon to reload a single zone."""

    # ── Validation / introspection ────────────────────────────────────────

    @abstractmethod
    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        """Validate a bundle before apply. Returns (ok, errors)."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Return a dict describing what this driver supports."""


__all__ = [
    "AclData",
    "BlocklistEntry",
    "ConfigBundle",
    "DNSDriver",
    "EffectiveBlocklistData",
    "RecordChange",
    "RecordData",
    "ServerOptions",
    "TrustAnchorData",
    "TsigKey",
    "ViewData",
    "ZoneData",
]
