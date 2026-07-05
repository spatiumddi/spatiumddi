"""Pydantic v2 schemas for the BGP Looking Glass operator CRUD + read surface.

Mirrors two conventions established elsewhere in the codebase:

* The ``asns`` router's CIDR/INET ``@field_validator(..., mode="before")``
  coercion — asyncpg returns ``CIDR``/``INET`` columns as
  ``ipaddress.IPv4Network`` / ``IPv4Address`` instances (etc.), not ``str``,
  so every read schema touching ``prefix`` / ``peer_address`` / ``next_hop``
  needs the coercion or the response 500s.
* The ``network`` router's Fernet ``_set`` boolean pattern — the plaintext
  ``md5_password`` is never returned; presence is signalled by
  ``PeerRead.md5_password_set``, computed inline from
  ``bool(peer.md5_password_encrypted)`` in the router.

See ``app.models.bgp_looking_glass`` for the underlying SQLAlchemy models.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# v1 scope — EVPN is a later phase; VPNv4/VPNv6 landed in Phase 6 (issue
# #566). Reject anything else at the API boundary rather than silently
# accepting a family the collector daemon can't render. Note: the
# collector-side GoBGP config render + RIB poll (agent/looking-glass/) is a
# follow-up — this widening lands the API + control-plane VRF RT-matching
# (app.services.looking_glass.vrf_match) now so peers can be configured and
# ext_communities-carrying routes cross-checked against VRF import/export
# targets ahead of the agent-side wire work.
_VALID_ADDRESS_FAMILIES = frozenset({"ipv4-unicast", "ipv6-unicast", "vpnv4", "vpnv6"})

# ``import_filter`` shape: {"mode": "accept_all"} or
# {"mode": "scope", "prefixes": [...]}. See ``BGPLGPeer.import_filter``.
_VALID_IMPORT_FILTER_MODES = frozenset({"accept_all", "scope"})

# 32-bit AS-number range (RFC 7607 reserves 0). Mirrors ``asns/router.py``'s
# ``_AS_MIN`` / ``_AS_MAX`` — kept as a local copy rather than importing from
# that router module, since ``local_asn`` / ``peer_asn`` here are plain
# BigIntegers, not necessarily backed by a tracked ``ASN`` row.
_AS_MIN = 1
_AS_MAX = 4_294_967_295


def _validate_asn(v: int) -> int:
    if not (_AS_MIN <= v <= _AS_MAX):
        raise ValueError(
            f"AS number must be between {_AS_MIN} and {_AS_MAX} (32-bit range; "
            "0 is reserved per RFC 7607 and not allowed)"
        )
    return v


def _validate_peer_address(v: str) -> str:
    try:
        ipaddress.ip_address(v)
    except ValueError as exc:
        raise ValueError(f"peer_address must be a bare IP address: {v!r}") from exc
    return v


def _validate_address_families(v: list[str]) -> list[str]:
    bad = sorted({af for af in v if af not in _VALID_ADDRESS_FAMILIES})
    if bad:
        raise ValueError(
            f"unsupported address_families {bad}; only "
            f"{sorted(_VALID_ADDRESS_FAMILIES)} are supported in this phase "
            "(EVPN deferred)"
        )
    return v


def _validate_import_filter(v: dict[str, Any]) -> dict[str, Any]:
    mode = v.get("mode")
    if mode not in _VALID_IMPORT_FILTER_MODES:
        raise ValueError(f"import_filter.mode must be one of {sorted(_VALID_IMPORT_FILTER_MODES)}")
    if mode == "scope" and not isinstance(v.get("prefixes"), list):
        raise ValueError("import_filter.prefixes must be a list when mode='scope'")
    return v


# ── Collector ─────────────────────────────────────────────────────────


class CollectorRead(BaseModel):
    """A ``LookingGlassCollector`` row — the agent-registration identity.

    Registration/heartbeat fields (``agent_id``, ``agent_registered``,
    ``last_seen_*``, ...) are agent-owned; operators may only rename,
    enable/disable, or delete (see ``CollectorUpdate``).
    """

    id: uuid.UUID
    name: str
    description: str
    host: str | None
    status: str
    enabled: bool
    agent_id: str | None
    agent_registered: bool
    agent_version: str | None
    last_seen_ip: str | None
    last_seen_at: datetime | None
    last_health_check_at: datetime | None
    appliance_id: uuid.UUID | None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}


class CollectorUpdate(BaseModel):
    """Operator rename / enable / disable.

    Registration + host/agent identity are agent-owned (set by the
    register/heartbeat endpoints, not here) — omitted fields are left
    untouched (``exclude_unset`` semantics in the router).
    """

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None


# ── Peer ──────────────────────────────────────────────────────────────


class PeerCreate(BaseModel):
    name: str
    collector_id: uuid.UUID
    local_asn: int
    peer_asn: int
    peer_address: str
    # Denormalised link to a tracked ASN row when one exists (raw
    # ``peer_asn`` stays the source of truth).
    matched_asn_id: uuid.UUID | None = None
    # Optional link to the SNMP-polled device this session terminates on.
    peer_router_id: uuid.UUID | None = None
    address_families: list[str] = Field(default_factory=lambda: ["ipv4-unicast"])
    # Plaintext MD5 password — Fernet-encrypted before storage, never
    # echoed back. See ``PeerRead.md5_password_set``.
    md5_password: str | None = None
    max_prefixes: int = 10000
    import_filter: dict[str, Any] = Field(default_factory=lambda: {"mode": "accept_all"})
    enabled: bool = True
    description: str = ""

    @field_validator("local_asn", "peer_asn")
    @classmethod
    def _v_asn(cls, v: int) -> int:
        return _validate_asn(v)

    @field_validator("peer_address")
    @classmethod
    def _v_peer_address(cls, v: str) -> str:
        return _validate_peer_address(v)

    @field_validator("address_families")
    @classmethod
    def _v_afs(cls, v: list[str]) -> list[str]:
        return _validate_address_families(v)

    @field_validator("import_filter")
    @classmethod
    def _v_filter(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _validate_import_filter(v)

    @field_validator("max_prefixes")
    @classmethod
    def _v_max_prefixes(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_prefixes must be >= 1")
        return v


class PeerUpdate(BaseModel):
    """Partial update. ``exclude_unset`` semantics in the router.

    ``md5_password``: a non-empty value rotates the stored ciphertext; an
    explicit empty string (``""``) clears it; omitting the field (the
    default ``None``) leaves the stored value untouched — mirrors
    ``NetworkDeviceUpdate``'s secret-rotation convention.
    """

    name: str | None = None
    collector_id: uuid.UUID | None = None
    local_asn: int | None = None
    peer_asn: int | None = None
    peer_address: str | None = None
    matched_asn_id: uuid.UUID | None = None
    peer_router_id: uuid.UUID | None = None
    address_families: list[str] | None = None
    md5_password: str | None = None
    max_prefixes: int | None = None
    import_filter: dict[str, Any] | None = None
    enabled: bool | None = None
    description: str | None = None

    @field_validator("local_asn", "peer_asn")
    @classmethod
    def _v_asn(cls, v: int | None) -> int | None:
        return _validate_asn(v) if v is not None else v

    @field_validator("peer_address")
    @classmethod
    def _v_peer_address(cls, v: str | None) -> str | None:
        return _validate_peer_address(v) if v is not None else v

    @field_validator("address_families")
    @classmethod
    def _v_afs(cls, v: list[str] | None) -> list[str] | None:
        return _validate_address_families(v) if v is not None else v

    @field_validator("import_filter")
    @classmethod
    def _v_filter(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_import_filter(v) if v is not None else v

    @field_validator("max_prefixes")
    @classmethod
    def _v_max_prefixes(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_prefixes must be >= 1")
        return v


class PeerRead(BaseModel):
    id: uuid.UUID
    name: str
    collector_id: uuid.UUID
    local_asn: int
    peer_asn: int
    peer_address: str
    matched_asn_id: uuid.UUID | None
    peer_router_id: uuid.UUID | None
    address_families: list[str]
    md5_password_set: bool
    max_prefixes: int
    import_filter: dict[str, Any]
    enabled: bool
    description: str
    # Runtime state (collector-reported via heartbeat).
    session_state: str
    uptime_started_at: datetime | None
    prefixes_received: int
    prefixes_accepted: int
    last_state_change: datetime | None
    last_flap_at: datetime | None
    rpki_invalid_count: int
    down_since: datetime | None
    created_at: datetime
    modified_at: datetime

    @field_validator("peer_address", mode="before")
    @classmethod
    def _coerce_peer_address(cls, v: Any) -> Any:
        # asyncpg returns INET columns as ipaddress.IPv4Address/IPv6Address.
        return str(v) if v is not None else v


# ── Sessions (per-peer state rollup) ───────────────────────────────────


class SessionRead(BaseModel):
    """One row per configured peer, joined with its owning collector.

    Drives the Sessions-tab feed — the runtime BGP-session state next to
    enough collector/peer context to render without a second round-trip.
    """

    peer_id: uuid.UUID
    peer_name: str
    collector_id: uuid.UUID
    collector_name: str
    collector_status: str
    local_asn: int
    peer_asn: int
    peer_address: str
    enabled: bool
    session_state: str
    uptime_started_at: datetime | None
    prefixes_received: int
    prefixes_accepted: int
    last_state_change: datetime | None
    last_flap_at: datetime | None
    rpki_invalid_count: int
    down_since: datetime | None

    @field_validator("peer_address", mode="before")
    @classmethod
    def _coerce_peer_address(cls, v: Any) -> Any:
        return str(v) if v is not None else v


# ── Routes (the learned RIB) ───────────────────────────────────────────


class RouteRead(BaseModel):
    id: uuid.UUID
    peer_id: uuid.UUID
    prefix: str
    origin_asn: int | None
    as_path: list[int]
    next_hop: str
    local_pref: int | None
    med: int | None
    communities: list[str]
    large_communities: list[str]
    ext_communities: list[str]
    # Route Distinguisher (RFC 4364) — non-empty only for vpnv4/vpnv6 paths;
    # part of the row's identity server-side (issue #566 Phase 6).
    route_distinguisher: str
    rpki_status: str
    is_best: bool
    matched_block_id: uuid.UUID | None
    matched_subnet_id: uuid.UUID | None
    matched_space_id: uuid.UUID | None
    matched_asn_id: uuid.UUID | None
    matched_vrf_id: uuid.UUID | None
    first_seen_at: datetime
    last_seen_at: datetime
    withdrawn_at: datetime | None
    flap_count: int
    detail: dict[str, Any] | None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("prefix", "next_hop", mode="before")
    @classmethod
    def _coerce_ip(cls, v: Any) -> Any:
        # asyncpg returns CIDR/INET columns as IPv4Network/IPv4Address (etc.)
        # instances. Coerce to str so JSON serialisation has something to work
        # with — see asns/router.py::ASNRpkiRoaRead._coerce_prefix.
        return str(v) if v is not None else v


class RouteListResponse(BaseModel):
    """Server-paginated envelope for ``GET /looking-glass/routes`` — mirrors
    ``AddressSearchResponse`` (``GET /ipam/addresses/search``)."""

    items: list[RouteRead]
    total: int
    limit: int
    offset: int


class RouteForIpResponse(BaseModel):
    """``GET /looking-glass/routes/for-ip`` — reverse LPM-by-address lookup.

    Mirrors the ``find_bgp_route_for_ip`` MCP tool's response shape (see
    ``app.services.ai.tools.bgp_lg``) so the two surfaces stay in sync.
    """

    ip: str
    found: bool
    route: RouteRead | None = None
    alternate_paths_count: int = 0


class LookingGlassDashboardSummary(BaseModel):
    """Single-shot rollup for the Dashboard's Looking Glass health card
    (issue #566 Phase 5). Deliberately NOT shaped like the Integrations
    dashboard tab's ``IntegrationPanel`` (``api/v1/dashboards/integrations.py``)
    — that shape keys off pull-reconciler fields
    (``last_synced_at``/``sync_interval_seconds``) that
    ``LookingGlassCollector`` doesn't have (it's agent-push-registered,
    like DNSServer/DHCPServer)."""

    peers_total: int
    peers_established: int
    peers_down: int
    routes_rpki_invalid: int
    routes_flapping: int


# ── VRF Route-Target cross-check (issue #566 Phase 6) ──────────────────


class VrfRtMatchRow(BaseModel):
    """One of a VRF's own import/export route targets that actually shows up
    on at least one currently-active learned route matched to that VRF."""

    route_target: str
    kind: str  # "import" | "export"
    matched_route_count: int


class VrfRtMatchSummary(BaseModel):
    """``GET /looking-glass/vrf-rt-matches/{vrf_id}`` — feeds the VRF detail
    page's "Learned VPN Routes" tab RT cross-check. ``matched_route_count``
    is every active route currently linked to this VRF (via
    ``BGPLGRoute.matched_vrf_id``, which Phase 6 sets from a Route-Target
    match when one exists — see ``app.services.looking_glass.vrf_match`` —
    falling back to the IPAM-effective VRF otherwise); ``route_targets``
    breaks that count down by which of the VRF's own RTs were actually seen
    in a matched route's ``ext_communities``."""

    vrf_id: uuid.UUID
    vrf_name: str
    matched_route_count: int
    route_targets: list[VrfRtMatchRow]


# ── Multicast <-> BGP reachability cross-reference (issue #566 Phase 6) ──


class DomainReachability(BaseModel):
    domain_id: uuid.UUID
    domain_name: str
    rp_address: str
    covering_route: RouteRead | None


class GroupSourceReachability(BaseModel):
    group_id: uuid.UUID
    group_name: str
    group_address: str
    source_subnet_id: uuid.UUID
    source_subnet: str
    covering_route: RouteRead | None


class MulticastReachabilityResponse(BaseModel):
    """``GET /looking-glass/multicast-reachability`` — read-only, computed on
    demand (no persisted columns; see
    ``app.services.looking_glass.reachability.multicast_bgp_reachability``).
    """

    domains: list[DomainReachability]
    groups: list[GroupSourceReachability]


# ── Peer detail rollup (issue #566 — Sessions-tab detail modal) ────────


class PeerDetailCollector(BaseModel):
    """Slim collector rollup for the peer detail modal — a subset of
    ``CollectorRead`` (drops the registration bookkeeping fields the modal
    has no use for)."""

    id: uuid.UUID
    name: str
    host: str | None
    status: str
    last_seen_ip: str | None
    agent_version: str | None
    enabled: bool

    model_config = {"from_attributes": True}


class PeerDetailMatchedAsn(BaseModel):
    id: uuid.UUID
    number: int
    name: str


class PeerDetailRouter(BaseModel):
    id: uuid.UUID
    name: str


class PeerDetailRpkiBreakdown(BaseModel):
    valid: int = 0
    invalid: int = 0
    unknown: int = 0


class PeerDetailOriginAsnCount(BaseModel):
    asn: int
    count: int


class PeerDetailCommunityCount(BaseModel):
    value: str
    count: int


class PeerDetailRouteStats(BaseModel):
    """Rollup over this peer's ``bgp_lg_route`` rows."""

    active_total: int
    withdrawn_total: int
    best_count: int
    rpki: PeerDetailRpkiBreakdown
    top_origin_asns: list[PeerDetailOriginAsnCount]
    top_communities: list[PeerDetailCommunityCount]
    # True when at least one active route carries a non-empty
    # ``route_distinguisher`` — i.e. this peer has VPNv4/VPNv6 (RFC 4364)
    # paths in its RIB, not just plain ipv4/ipv6-unicast.
    has_vpn_routes: bool
    # First ~8 active routes, for the modal's preview table.
    sample_routes: list[RouteRead]


class PeerDetailAlert(BaseModel):
    """One open ``AlertEvent`` whose rule references this peer or one of
    its learned routes (``bgp_lg_*`` rule types only)."""

    severity: str
    message: str
    rule_type: str
    fired_at: datetime


class PeerDetailResponse(BaseModel):
    """``GET /looking-glass/peers/{peer_id}/detail`` — the rich rollup
    backing the Sessions-tab peer detail modal. Composes the peer's own
    config + runtime state (``peer``, already carrying every session-state
    field — see ``PeerRead``) with its collector, matched ASN/router links,
    an aggregate view over its learned RIB, and any open ``bgp_lg_*``
    alerts that reference it."""

    peer: PeerRead
    collector: PeerDetailCollector
    matched_asn: PeerDetailMatchedAsn | None
    peer_router: PeerDetailRouter | None
    route_stats: PeerDetailRouteStats
    active_alerts: list[PeerDetailAlert]


# ── Route detail rollup (issue #566 — Routes-tab detail modal) ─────────


class RouteDetailPath(BaseModel):
    """One peer's Adj-RIB-In path for the prefix, enriched with the
    announcing peer + collector name so the modal's comparison table
    doesn't need a second round-trip per row."""

    route_id: uuid.UUID
    peer_id: uuid.UUID
    peer_name: str
    collector_name: str
    origin_asn: int | None
    next_hop: str
    local_pref: int | None
    med: int | None
    as_path: list[int]
    communities: list[str]
    large_communities: list[str]
    ext_communities: list[str]
    route_distinguisher: str
    rpki_status: str
    is_best: bool
    first_seen_at: datetime
    last_seen_at: datetime
    flap_count: int
    withdrawn_at: datetime | None
    matched_subnet_id: uuid.UUID | None
    matched_block_id: uuid.UUID | None
    matched_space_id: uuid.UUID | None
    matched_asn_id: uuid.UUID | None
    matched_vrf_id: uuid.UUID | None

    @field_validator("next_hop", mode="before")
    @classmethod
    def _coerce_next_hop(cls, v: Any) -> Any:
        # asyncpg returns INET columns as ipaddress.IPv4Address/IPv6Address.
        return str(v) if v is not None else v


class RouteDetailSummary(BaseModel):
    """Server-computed rollup over every path returned for the prefix — the
    headline signal the modal leads with. ``anycast_candidate`` (same
    prefix, multiple routers, single origin) and ``multi_origin`` (more
    than one origin ASN announcing it) are mutually informative but NOT
    mutually exclusive: an anycast deployment is multi-homed by design
    (``anycast_candidate``), while ``multi_origin`` on its own — regardless
    of peer count — is the hijack/leak signal."""

    path_count: int
    peer_count: int
    distinct_origin_asns: list[int]
    multi_origin: bool
    anycast_candidate: bool
    rpki: PeerDetailRpkiBreakdown
    # origin ASN -> tracked ASN row's name, only for origins that match a
    # row in the ASN catalog (unmatched origins are simply absent here).
    origin_names: dict[int, str]


class RouteDetailIpamContext(BaseModel):
    """The covering IPAM object(s) for this prefix, plus the ASN/VRF a path
    resolved against — read off the ``matched_*_id`` columns already
    populated at ingest by ``app.services.looking_glass.ipam_link`` (and,
    for ``vrf_id``, by the Phase 6 Route-Target cross-check). Preference is
    given to the current best path's linkage when paths disagree (VPNv4/
    VPNv6's per-route-target VRF match can legitimately differ path to
    path); falls back to the first path carrying a non-NULL value."""

    subnet_id: uuid.UUID | None = None
    subnet_name: str | None = None
    block_id: uuid.UUID | None = None
    block_name: str | None = None
    space_id: uuid.UUID | None = None
    space_name: str | None = None
    asn_id: uuid.UUID | None = None
    asn_number: int | None = None
    asn_name: str | None = None
    vrf_id: uuid.UUID | None = None
    vrf_name: str | None = None


class RouteDetailResponse(BaseModel):
    """``GET /looking-glass/routes/detail`` — the rich per-prefix rollup
    backing the Routes-tab detail modal (issue #566). Unlike
    ``GET /routes/by-prefix`` (a flat ``list[RouteRead]``), this composes
    every active (or, with ``?withdrawn=true``, every) path for the exact
    prefix with peer/collector names, a server-computed summary the modal
    leads its banner with, and the covering IPAM/ASN/VRF context."""

    prefix: str
    paths: list[RouteDetailPath]
    summary: RouteDetailSummary
    ipam: RouteDetailIpamContext
