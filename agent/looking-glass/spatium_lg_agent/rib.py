"""RIB + per-neighbor session-state poller.

Polls each peer's **Adj-RIB-In** (via ``gobgp neighbor <peer_address>
adj-in -j``, one call per configured neighbor) — NOT the post-best-path
Loc-RIB (``gobgp global rib``). This distinction is load-bearing for the
feature: the Loc-RIB has already run best-path selection and dropped every
non-winning path, so two peers announcing the same prefix collapse to one
row there. The Looking Glass is specifically for seeing *every* path a
prefix was learned over, side by side across peers, so it must read the
raw received table (Adj-RIB-In) for each neighbor before selection.

Adj-RIB-In reports ``best: false`` on every path (verified live — it is
the raw received table, pre-selection), so the ``is_best`` flag the
backend surfaces is cross-referenced from a single supplementary
``gobgp global rib -j`` poll that yields the winning
``(peer_address, prefix, next_hop)`` tuples. That global-rib poll is used
ONLY to mark winners — route attribution comes entirely from which
neighbor's adj-in a path was read from, so there is no fragile
peer-address→peer-id guessing on the global-rib side anymore. A failed
best-set poll is non-fatal (routes still push, just with ``is_best`` all
false that cycle).

Feeds two consumers:

  - :class:`spatium_lg_agent.heartbeat.HeartbeatClient` — session state,
    uptime, prefix counts (``heartbeat.peer_states``, keyed by peer_id).
  - ``POST /api/v1/looking-glass/agents/routes`` — the Adj-RIB-In push,
    ONE call per peer (see :meth:`RibPoller._push_one_peer` for the exact
    JSON contract — matches ``backend/app/api/v1/looking_glass/agents.py``'s
    ``RoutesPushRequest``/``RouteEntry`` schemas exactly, both of which
    carry ``model_config = ConfigDict(extra="forbid")``).

v1 design (issue #566 plan decision D6): full-snapshot push + periodic
reconcile, no per-UPDATE delta streaming. Every poll cycle pushes a
snapshot for EVERY currently-configured peer — even an empty one (0
routes) — because the backend's own zero-wire floor guard
(``services.looking_glass.routes_ingest.ingest_routes``) needs to see the
call happen to reason about it (it compares against the peer's own
persisted ``prefixes_received`` from the last heartbeat to decide whether
an empty snapshot is a genuine full withdrawal or a collector hiccup); a
peer the collector silently skips calling for is a peer the backend never
gets a chance to reconcile at all.

Field-name/shape notes below (marked "verified live") come from smoke
testing against a real gobgpd v4.5.0 binary (3-node topology) during
development of this module — GoBGP's CLI JSON output is not treated as a
stable API by upstream and has changed across major versions, so anything
NOT marked verified is a best-effort parse, logged and dropped rather than
guessed at.
"""

from __future__ import annotations

import ipaddress
import json
import random
import threading
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from .cache import touch_ready_marker
from .config import AgentConfig
from .gobgp import GoBGPCliError, run_gobgp_cli

log = structlog.get_logger(__name__)

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# RFC 4271 §8.2.2 FSM state numbering — GoBGP uses the same 1..6 range
# (verified live: a peer shown as "Active" in ``gobgp neighbor``'s
# plain-text view reported ``session_state: 3`` in ``-j`` output; an
# Established peer reported 6).
_SESSION_STATES = {
    1: "idle",
    2: "connect",
    3: "active",
    4: "opensent",
    5: "openconfirm",
    6: "established",
}

# RFC 4271 §4.3 BGP path-attribute type codes. Types 1/2/3/4/8 shapes are
# verified live against gobgpd v4.5.0; 5/7/16/32 follow the RFC + gobgp
# source field names but were not exercised by a live session in this
# module's own development (EBGP strips LOCAL_PREF on send, and no
# ext/large-community test route was advertised) — anything that doesn't
# parse cleanly is logged and dropped, never guessed at.
_ATTR_ORIGIN = 1
_ATTR_AS_PATH = 2
_ATTR_NEXT_HOP = 3
_ATTR_MED = 4
_ATTR_LOCAL_PREF = 5
_ATTR_AGGREGATOR = 7
_ATTR_COMMUNITY = 8
_ATTR_EXT_COMMUNITY = 16
_ATTR_LARGE_COMMUNITY = 32


def _decode_community(raw: int) -> str:
    """RFC 1997 4-byte community -> "ASN:VALUE".

    Verified live: advertising community ``65001:100`` round-tripped to
    raw uint32 ``4259905636 == (65001 << 16) | 100``.
    """
    return f"{(raw >> 16) & 0xFFFF}:{raw & 0xFFFF}"


def _decode_large_community(entry: dict[str, Any]) -> str | None:
    """RFC 8092 large community -> "GLOBAL:LOCAL1:LOCAL2".

    Key spelling NOT verified live (see module docstring) — tries the
    plausible snake_case spellings consistent with everything else this
    gobgp version emits, and gives up (returning None) rather than
    guessing wrong.
    """
    global_admin = entry.get("global_admin", entry.get("global_administrator"))
    local1 = entry.get("local_data1", entry.get("local_data_part1"))
    local2 = entry.get("local_data2", entry.get("local_data_part2"))
    if global_admin is None or local1 is None or local2 is None:
        return None
    return f"{global_admin}:{local1}:{local2}"


def _flatten_as_path(as_path_attr: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for seg in as_path_attr.get("as_paths") or []:
        out.extend(int(a) for a in seg.get("asns") or [])
    return out


def _path_next_hop(path: dict[str, Any]) -> str | None:
    for attr in path.get("attrs") or []:
        if attr.get("type") == _ATTR_NEXT_HOP:
            return attr.get("nexthop") or attr.get("next_hop")
    return None


def _iter_rib_items(raw: Any) -> list[tuple[Any, Any]]:
    """Normalise a ``gobgp ... -j`` RIB document to ``[(prefix, paths)]``.

    Verified live: both ``gobgp global rib -j`` and
    ``gobgp neighbor <addr> adj-in -j`` emit ``{"<prefix>": [path, ...]}``.
    Tolerate a flat ``[{"prefix": ..., "paths": [...]}]`` too, in case a
    future/older CLI version changes shape.
    """
    if isinstance(raw, dict):
        return list(raw.items())
    if isinstance(raw, list):
        return [
            (entry.get("prefix"), entry.get("paths", []))
            for entry in raw
            if isinstance(entry, dict)
        ]
    return []


def _extract_best_set(raw: Any) -> set[tuple[str, str, str]]:
    """Pull the winning ``(peer_address, prefix, next_hop)`` tuples out of a
    ``gobgp global rib -j`` document.

    Adj-RIB-In reports ``best: false`` universally (it is pre-selection),
    so ``is_best`` on the pushed rows is cross-referenced against this
    Loc-RIB winner set. Keyed on ``peer_address`` too (not just
    ``(prefix, next_hop)``) so that in the rare case two peers announce the
    same prefix with an unchanged next-hop, only the actual Loc-RIB winner
    is flagged.
    """
    best: set[tuple[str, str, str]] = set()
    for prefix, paths in _iter_rib_items(raw):
        if not prefix or not isinstance(paths, list):
            continue
        for path in paths:
            if not isinstance(path, dict) or not path.get("best"):
                continue
            peer_address = (
                path.get("peer-address")
                or path.get("neighbor-ip")
                or path.get("neighbor_ip")
            )
            next_hop = _path_next_hop(path)
            if peer_address and next_hop:
                best.add((str(peer_address), str(prefix), str(next_hop)))
    return best


def _parse_path(
    prefix: str,
    path: dict[str, Any],
    peer_id: str,
    peer_address: str,
    best_set: set[tuple[str, str, str]],
) -> dict[str, Any] | None:
    """Parse one Adj-RIB-In path entry into an internal route dict keyed on
    the fields the control-plane's ``RouteEntry`` schema accepts, plus
    ``peer_id`` (stripped back out before the wire push — see
    :meth:`RibPoller._push_routes`).

    ``peer_id`` / ``peer_address`` are known from which neighbor's adj-in
    this path was read from (no attribution guessing). Returns ``None`` if
    the path has no ``next_hop`` (``RouteEntry.next_hop`` is required,
    non-nullable — pushing ``null`` there would 422 the whole per-peer
    request). ``is_best`` is set from the Loc-RIB ``best_set``, since
    adj-in itself always reports ``best: false``.
    """
    next_hop: str | None = None
    origin_asn: int | None = None
    as_path: list[int] = []
    med: int | None = None
    local_pref: int | None = None
    communities: list[str] = []
    large_communities: list[str] = []
    ext_communities: list[str] = []

    for attr in path.get("attrs") or []:
        t = attr.get("type")
        if t == _ATTR_AS_PATH:
            as_path = _flatten_as_path(attr)
        elif t == _ATTR_NEXT_HOP:
            next_hop = attr.get("nexthop") or attr.get("next_hop")
        elif t == _ATTR_MED:
            med = attr.get("metric", attr.get("value"))
        elif t == _ATTR_LOCAL_PREF:
            local_pref = attr.get("value")
        elif t == _ATTR_COMMUNITY:
            for c in attr.get("communities") or []:
                try:
                    communities.append(_decode_community(int(c)))
                except (TypeError, ValueError):
                    # Skip a non-integer/garbage community value rather than
                    # drop the whole route; the rest of its attributes are
                    # still worth ingesting.
                    continue
        elif t == _ATTR_LARGE_COMMUNITY:
            for lc in attr.get("large_communities") or []:
                decoded = _decode_large_community(lc) if isinstance(lc, dict) else None
                if decoded:
                    large_communities.append(decoded)
                else:
                    log.debug("lg_large_community_unparsed", raw=lc)
        elif t == _ATTR_EXT_COMMUNITY:
            raw_ext = attr.get("communities") or attr.get("extended_communities") or []
            ext_communities.extend(str(v) for v in raw_ext)
        elif t in (_ATTR_ORIGIN, _ATTR_AGGREGATOR):
            pass  # not modeled on bgp_lg_route — informational only
        else:
            log.debug("lg_path_attr_unparsed", attr_type=t)

    if not next_hop:
        log.warning("lg_route_missing_next_hop", peer_id=peer_id, prefix=prefix)
        return None

    if as_path:
        origin_asn = as_path[-1]

    return {
        "peer_id": peer_id,
        "prefix": prefix,
        "next_hop": next_hop,
        "origin_asn": origin_asn,
        "as_path": as_path,
        "med": med,
        "local_pref": local_pref,
        "communities": communities,
        "large_communities": large_communities,
        "ext_communities": ext_communities,
        "is_best": (peer_address, prefix, next_hop) in best_set,
    }


def _parse_adj_in(
    raw: Any,
    peer_id: str,
    peer_address: str,
    best_set: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Parse one neighbor's ``adj-in -j`` document into route dicts, all
    attributed to that neighbor's ``peer_id`` (known — no guessing)."""
    routes: list[dict[str, Any]] = []
    for prefix, paths in _iter_rib_items(raw):
        if not prefix or not isinstance(paths, list):
            continue
        for path in paths:
            if not isinstance(path, dict):
                continue
            row = _parse_path(str(prefix), path, peer_id, peer_address, best_set)
            if row is not None:
                routes.append(row)
    return routes


def _prefix_in_scope(prefix: str, scope_nets: list[_IPNetwork]) -> bool:
    """True if ``prefix`` is equal to or more-specific than any scope net.

    A route is "in scope" when its prefix falls WITHIN one of the
    operator's scope prefixes (``subnet_of`` — a network is a subnet of
    itself, so an exact match counts). Same-version compares only. An
    unparseable prefix is out of scope (dropped) — it would fail the
    backend's CIDR validation on push anyway.
    """
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return False
    for s in scope_nets:
        # isinstance (not a .version compare) so the concrete network type
        # is narrowed for the version-specific subnet_of overload.
        if isinstance(net, ipaddress.IPv4Network) and isinstance(
            s, ipaddress.IPv4Network
        ):
            if net.subnet_of(s):
                return True
        elif isinstance(net, ipaddress.IPv6Network) and isinstance(
            s, ipaddress.IPv6Network
        ):
            if net.subnet_of(s):
                return True
    return False


def _apply_scope(
    routes: list[dict[str, Any]], scope_nets: list[_IPNetwork]
) -> list[dict[str, Any]]:
    """Filter a peer's parsed adj-in rows down to the scoped prefixes.

    See ``gobgp.render_config``'s docstring for why the scope is enforced
    here (agent-side, on the raw Adj-RIB-In) rather than as a GoBGP import
    policy. An empty ``scope_nets`` means "report nothing" — the literal
    meaning of restricting to zero prefixes (already warned about at
    scope-map build time in ``gobgp.peer_import_scopes``)."""
    return [r for r in routes if _prefix_in_scope(str(r["prefix"]), scope_nets)]


def _sum_afi_safi_counts(neighbor: dict[str, Any], key: str) -> int:
    """Verified live: each ``afi_safis[].state`` entry carries ``received``
    / ``accepted`` int counters once a session reaches Established."""
    total = 0
    for afi_safi in neighbor.get("afi_safis") or []:
        state = afi_safi.get("state") or {}
        try:
            total += int(state.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _epoch_to_iso(seconds: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(seconds), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _parse_neighbor(neighbor: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Parse one ``gobgp neighbor -j`` entry into
    ``(peer_address, state_dict)``.

    ``state_dict`` keys match ``PeerStateReport`` (see
    ``backend/app/api/v1/looking_glass/agents.py``) exactly:
    ``session_state`` / ``uptime_started_at`` / ``prefixes_received`` /
    ``prefixes_accepted`` / ``last_state_change`` / ``last_flap_at`` — all
    but ``session_state`` optional (``None`` when unknown), and
    ``uptime_started_at`` / ``last_state_change`` / ``last_flap_at`` are
    ISO-8601 datetimes, not durations.

    GoBGP's ``timers.state`` carries an epoch-timestamp ``downtime.seconds``
    even for a peer that has never gone down (verified live — present
    alongside ``uptime.seconds`` with the identical value on a peer's
    first-ever Established transition), so it's treated here as "the
    timestamp of the most recent FSM state change" for ``last_state_change``
    unconditionally, and additionally surfaced as ``last_flap_at`` only
    while the peer is NOT currently established (i.e. "down since") — a
    currently-Established session isn't presently flapping.
    """
    conf = neighbor.get("conf") or {}
    state = neighbor.get("state") or {}
    address = state.get("neighbor_address") or conf.get("neighbor_address")
    if not address:
        return None, {}

    raw_session_state = state.get("session_state")
    session_state = "unknown"
    if raw_session_state is not None:
        try:
            session_state = _SESSION_STATES.get(int(raw_session_state), "unknown")
        except (TypeError, ValueError):
            session_state = "unknown"

    timers = (neighbor.get("timers") or {}).get("state") or {}

    uptime_started_at = None
    if session_state == "established":
        up = timers.get("uptime")
        if isinstance(up, dict):
            uptime_started_at = _epoch_to_iso(up.get("seconds"))

    down = timers.get("downtime")
    last_state_change = (
        _epoch_to_iso(down.get("seconds")) if isinstance(down, dict) else None
    )
    last_flap_at = last_state_change if session_state != "established" else None

    return str(address), {
        "session_state": session_state,
        "uptime_started_at": uptime_started_at,
        "prefixes_received": _sum_afi_safi_counts(neighbor, "received"),
        "prefixes_accepted": _sum_afi_safi_counts(neighbor, "accepted"),
        "last_state_change": last_state_change,
        "last_flap_at": last_flap_at,
    }


class RibPoller:
    def __init__(self, cfg: AgentConfig, token_ref: list[str], heartbeat: Any):
        self.cfg = cfg
        self.token_ref = token_ref
        self.heartbeat = heartbeat
        self._stop = threading.Event()
        # peer_address -> peer_id, refreshed by SyncLoop after every
        # bundle apply (including the pre-first-poll cache preload) so
        # this poller can attribute paths + neighbor rows to the right
        # ``bgp_lg_peer`` row even before its own first successful poll.
        self._peer_addr_map: dict[str, str] = {}
        self._known_peer_ids: set[str] = set()
        # peer_id -> [scope networks]; a peer absent from the map has no
        # import scope (report everything). See gobgp.peer_import_scopes.
        self._scope_map: dict[str, list[_IPNetwork]] = {}

    def set_peers(
        self,
        peer_addr_map: dict[str, str],
        scope_map: dict[str, list[_IPNetwork]] | None = None,
    ) -> None:
        self._peer_addr_map = dict(peer_addr_map)
        self._known_peer_ids = set(self._peer_addr_map.values())
        self._scope_map = dict(scope_map or {})

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.cfg.control_plane_url,
            verify=self.cfg.httpx_verify(),
            timeout=30.0,
        )

    def _poll_best_set(self) -> set[tuple[str, str, str]]:
        """Loc-RIB winners as ``(peer_address, prefix, next_hop)`` tuples,
        used only to mark ``is_best`` on the adj-in rows (adj-in itself
        always reports ``best: false``). Non-fatal: on failure return an
        empty set and every route pushes this cycle with ``is_best=False``
        — a cosmetic degradation, never a route drop."""
        best: set[tuple[str, str, str]] = set()
        for afi in ("ipv4", "ipv6"):
            try:
                raw_text = run_gobgp_cli(self.cfg, "global", "rib", "-a", afi, "-j")
            except GoBGPCliError as e:
                log.warning("lg_best_set_poll_failed", afi=afi, error=str(e))
                continue
            try:
                raw = json.loads(raw_text) if raw_text.strip() else {}
            except ValueError:
                log.warning("lg_best_set_poll_bad_json", afi=afi)
                continue
            best |= _extract_best_set(raw)
        return best

    def _poll_adj_in(
        self, peer_address: str, peer_id: str, best_set: set[tuple[str, str, str]]
    ) -> list[dict[str, Any]]:
        """Poll one neighbor's Adj-RIB-In (every received path, pre-best-
        path-selection) across v4 + v6. A per-AFI CLI failure (e.g. the
        session isn't Established yet) is logged and skipped — the peer
        still gets an empty push, which the backend zero-wire guard
        handles against its last-heartbeated ``prefixes_received``.

        When the peer has an import scope (``import_filter.mode ==
        "scope"``), the parsed rows are filtered down to the scoped
        prefixes here — see ``_apply_scope`` / ``gobgp.render_config``'s
        docstring for why the scope is enforced agent-side."""
        routes: list[dict[str, Any]] = []
        for afi in ("ipv4", "ipv6"):
            try:
                raw_text = run_gobgp_cli(
                    self.cfg, "neighbor", peer_address, "adj-in", "-a", afi, "-j"
                )
            except GoBGPCliError as e:
                log.warning(
                    "lg_adj_in_poll_failed",
                    peer_id=peer_id,
                    peer_address=peer_address,
                    afi=afi,
                    error=str(e),
                )
                continue
            try:
                raw = json.loads(raw_text) if raw_text.strip() else {}
            except ValueError:
                log.warning("lg_adj_in_poll_bad_json", peer_id=peer_id, afi=afi)
                continue
            routes.extend(_parse_adj_in(raw, peer_id, peer_address, best_set))
        scope_nets = self._scope_map.get(peer_id)
        if scope_nets is not None:
            before = len(routes)
            routes = _apply_scope(routes, scope_nets)
            if before != len(routes):
                log.info(
                    "lg_adj_in_scope_filtered",
                    peer_id=peer_id,
                    kept=len(routes),
                    dropped=before - len(routes),
                )
        return routes

    def _poll_neighbors(self) -> dict[str, dict[str, Any]]:
        """Returns ``peer_id -> state_dict`` for every neighbor gobgpd
        knows about that we can attribute to a currently-configured peer.
        """
        try:
            raw_text = run_gobgp_cli(self.cfg, "-j", "neighbor")
        except GoBGPCliError as e:
            log.warning("lg_neighbor_poll_failed", error=str(e))
            return {}
        try:
            raw = json.loads(raw_text) if raw_text.strip() else []
        except ValueError:
            log.warning("lg_neighbor_poll_bad_json")
            return {}
        if not isinstance(raw, list):
            raw = [raw]

        out: dict[str, dict[str, Any]] = {}
        for neighbor in raw:
            if not isinstance(neighbor, dict):
                continue
            address, state = _parse_neighbor(neighbor)
            if not address:
                continue
            peer_id = self._peer_addr_map.get(address)
            if not peer_id:
                continue
            out[peer_id] = state
        return out

    def _push_one_peer(self, peer_id: str, routes: list[dict[str, Any]]) -> None:
        """POST one peer's full-snapshot routes push.

        Contract — ``POST /api/v1/looking-glass/agents/routes`` (matches
        ``RoutesPushRequest``/``RouteEntry`` exactly, both
        ``extra="forbid"``)::

            {
              "peer_id": "<uuid>",
              "snapshot": true,
              "routes": [
                {
                  "prefix": "203.0.113.0/24", "next_hop": "203.0.113.1",
                  "origin_asn": 65001, "as_path": [65001, 65002],
                  "local_pref": 100, "med": 0,
                  "communities": ["65001:100"], "large_communities": [],
                  "ext_communities": [], "is_best": true
                },
                ...
              ]
            }

        Pushed for EVERY known peer every cycle, even with an empty
        ``routes`` list — see module docstring for why an empty push must
        still happen rather than being skipped.
        """
        body = {"peer_id": peer_id, "snapshot": True, "routes": routes}
        try:
            with self._client() as c:
                resp = c.post(
                    "/api/v1/looking-glass/agents/routes",
                    json=body,
                    headers={"Authorization": f"Bearer {self.token_ref[0]}"},
                )
            if resp.status_code != 200:
                log.warning(
                    "lg_routes_push_failed",
                    peer_id=peer_id,
                    status=resp.status_code,
                    body=resp.text[:300],
                )
        except httpx.HTTPError as e:
            log.warning("lg_routes_push_http_error", peer_id=peer_id, error=str(e))

    def _push_routes(self, routes: list[dict[str, Any]]) -> None:
        by_peer: dict[str, list[dict[str, Any]]] = {
            pid: [] for pid in self._known_peer_ids
        }
        for r in routes:
            peer_id = r.get("peer_id")
            bucket = by_peer.get(peer_id) if peer_id else None
            if bucket is None:
                continue
            bucket.append(
                {
                    "prefix": r["prefix"],
                    "next_hop": r["next_hop"],
                    "origin_asn": r.get("origin_asn"),
                    "as_path": r.get("as_path") or [],
                    "local_pref": r.get("local_pref"),
                    "med": r.get("med"),
                    "communities": r.get("communities") or [],
                    "large_communities": r.get("large_communities") or [],
                    "ext_communities": r.get("ext_communities") or [],
                    "is_best": bool(r.get("is_best", False)),
                }
            )
        for peer_id, peer_routes in by_peer.items():
            self._push_one_peer(peer_id, peer_routes)

    def _poll_once(self) -> None:
        # Readiness = "the poll loop is running and gobgpd is reachable" —
        # an internal-state condition, NOT "the control plane 200'd our
        # push" (mirrors the DNS agent's ``_touch_ready_marker``). A fresh
        # collector's normal steady state is ZERO peers configured, so the
        # marker MUST be stamped in that path too; otherwise the K8s
        # readinessProbe never flips and the pod is NotReady forever. Stamp
        # first, before the no-peers early return.
        touch_ready_marker(self.cfg.state_dir)
        if not self._known_peer_ids:
            # No peers configured — nothing to poll or push, but we are
            # ready (see above).
            return
        neighbor_states = self._poll_neighbors()
        self.heartbeat.peer_states = neighbor_states
        # Adj-RIB-In per neighbor (all received paths), with a single
        # supplementary Loc-RIB poll to mark best-path winners (see module
        # docstring). Route attribution is by which neighbor we polled — no
        # global-rib peer-address guessing.
        best_set = self._poll_best_set()
        routes: list[dict[str, Any]] = []
        for peer_address, peer_id in self._peer_addr_map.items():
            routes.extend(self._poll_adj_in(peer_address, peer_id, best_set))
        self._push_routes(routes)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — never let the poll loop die
                log.exception("lg_rib_poll_cycle_failed")
            interval = self.cfg.rib_poll_interval + random.uniform(-3, 3)
            self._stop.wait(timeout=max(5.0, interval))


__all__ = ["RibPoller"]
