"""GoBGP config rendering + live-apply for the receive-only BGP collector.

===========================================================================
RECEIVE-ONLY SAFETY INVARIANT (issue #566) — READ BEFORE TOUCHING THIS FILE
===========================================================================
The BGP Looking Glass collector is a pure sink: it must NEVER advertise a
route back to the operator's network, under any circumstance. This module
is the single place that enforcement is implemented, and any change to
:func:`render_config` or :func:`_render_neighbor` is a first-class review
gate for that guarantee.

**Enforcement point**: ``render_config()`` sets
``global.apply-policy.config.default-export-policy = "reject-route"`` at
the GLOBAL (daemon-wide) level, and never populates an
``export-policy-list`` anywhere. :func:`_assert_receive_only` re-checks
this on every render and raises :class:`ReceiveOnlyViolation` — a hard
crash, not a soft warning — if it is ever violated.

**Why the GLOBAL level, not a per-neighbor ``apply-policy`` block**: this
was verified empirically against a live gobgpd v4.5.0 binary with a 3-node
B->A->C topology (B advertises a prefix, A is the collector-under-test
peered receive-only with both B and C, C is a stand-in for "the operator's
other router"). Setting ``default-export-policy: reject-route`` only under
``neighbors[].apply-policy`` did **not** stop A from re-advertising the
prefix it learned from B to C — gobgpd's static config loader
(``pkg/config/config.go``'s ``assignGlobalpolicy``, called from
``InitialConfig``/``UpdateConfig``) only wires ``global.apply-policy``
into a real policy assignment at daemon (re)start; for a normal
(non-route-server) peer, every neighbor's adj-rib-out lookup resolves to
the shared ``GLOBAL_RIB_NAME`` policy assignment
(``pkg/server/peer.go``'s ``peer.TableID()``), so a per-neighbor
``apply-policy`` block is parsed into the config struct but is otherwise
dead configuration for this code path. Moving the identical
``default-export-policy: reject-route`` setting to ``global.apply-policy``
blocked the leak completely (confirmed: the downstream peer's RIB stayed
empty and its "#Received" counter for the collector stayed 0). We still
set the per-neighbor block too, defensively — it costs nothing and
protects against a future gobgpd version that starts honoring it per-peer.

The other load-bearing safety knob is the per-peer, per-AFI
``prefix-limit.config.max-prefixes`` (issue #566 decision D4) — also
verified empirically: exceeding the configured cap logs
``"prefix limit reached"`` and gobgpd stops *accepting* further prefixes
from that peer (a soft cap that protects the RIB from a full-table blow-up
without tearing down the BGP session).

Config is written as JSON (gobgpd's ``--config-type json`` — JSON is a
strict subset of YAML 1.2, so writing JSON avoids a PyYAML dependency
without losing anything gobgpd's config loader understands) and applied
live via ``SIGHUP`` (gobgpd's own documented reload mechanism —
``cmd/gobgpd/main.go`` calls ``config.ReadConfigFile`` + ``UpdateConfig``
on receipt; unlike an unhandled signal this does NOT terminate the
process). The container also runs gobgpd with ``--config-auto-reload``
(fsnotify-based, see the image entrypoint) as a second, independent
delivery path for the same reload — belt-and-braces, mirroring the
ETag-poll-is-authoritative / wake-is-advisory pattern used elsewhere in
the fleet (CLAUDE.md cross-cutting pattern #2).
"""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import socket
import subprocess
import threading
import time
from typing import Any

import structlog

from .cache import save_rendered_gobgpd
from .config import AgentConfig

log = structlog.get_logger(__name__)

# Parsed IP network types the scope filter works with.
_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# gobgpd's own SIGHUP-triggered reload only re-reads its config file — it
# does not fsnotify-watch by itself unless started with this flag. We pass
# BOTH: ``--config-auto-reload`` as an independent, automatic delivery
# path, and an explicit SIGHUP (see ``reload``) as a deterministic one the
# Python agent can rely on right after writing a new file. Neither is the
# sole path (mirrors the ETag-poll-is-authoritative / wake-is-advisory
# pattern used elsewhere in the fleet).
_GOBGPD_ARGS = ("-t", "json", "-l", "info", "--config-auto-reload", "--pprof-disable")

# v1 scope (issue #566 Phase 1+2) — VPNv4/VPNv6 + multicast address
# families are explicitly out of scope (see the implementation plan §2,
# deferred-phase table). Anything else is logged + skipped rather than
# failing the whole peer, since a single bad address-family string must
# not take the collector offline.
_KNOWN_AFI_SAFIS = {"ipv4-unicast", "ipv6-unicast"}
_DEFAULT_BGP_PORT = 179
_SHUTDOWN_THRESHOLD_PCT = 90
_DEFAULT_MAX_PREFIXES = 10000  # issue #566 decision D4
# The collector's own daemon-wide fallback identity when no peer is
# configured yet (idle state — matches images/gobgp/gobgpd-idle.json's
# baked-in placeholder exactly, so a freshly-applied empty bundle renders
# to the same values the image already boots with). Neither value carries
# operational meaning for a receive-only eBGP collector with no
# route-reflection/iBGP in play — GoBGP just requires *a* syntactically
# valid AS + router-id to start.
_PLACEHOLDER_AS = 4200000000  # RFC 6996 private-use 4-byte ASN range start
_PLACEHOLDER_ROUTER_ID = "192.0.2.1"  # RFC 5737 TEST-NET-1 (documentation-only)


class GoBGPCliError(RuntimeError):
    """Raised when the ``gobgp`` CLI exits non-zero or can't be reached."""


class ReceiveOnlyViolation(RuntimeError):
    """A bug turned the collector into a transit path.

    This must never fire in production — it is a hard fail-safe, not a
    soft warning. See the module docstring.
    """


def _derive_router_id() -> str:
    """Best-effort local router-id when the bundle carries none.

    Neither ``LookingGlassCollector`` nor ``BGPLGPeer`` carries a
    router-id field on the control-plane side (see
    ``backend/app/models/bgp_looking_glass.py``) — GoBGP requires a
    syntactically valid one to start, but for a receive-only eBGP
    collector with no route-reflection/iBGP in play, its exact value has
    no operational meaning (it never triggers a BGP identifier tie-break
    or otherwise resolves any protocol comparison here). Try to derive
    something unique-ish per host for cosmetic/debug value; fall back to
    the same documentation-only placeholder the baked idle image ships.
    """
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return _PLACEHOLDER_ROUTER_ID


def render_config(bundle: dict[str, Any]) -> dict[str, Any]:
    """Render a gobgpd JSON config document from a control-plane peer bundle.

    Bundle shape — the ``bundle`` object nested inside
    ``GET /api/v1/looking-glass/agents/config``'s response (see
    ``backend/app/services/looking_glass/config_bundle.py``'s
    ``LGConfigBundle``/``LGPeerDef``; ``sync.py`` unwraps the outer
    ``{collector_id, etag, bundle}`` envelope before calling this)::

        {
          "collector_name": "...",
          "peers": [
            {
              "peer_id": "...", "name": "...",
              "peer_address": "203.0.113.1",
              "peer_asn": 65001,
              "local_asn": 65000,             # REQUIRED on every peer row
              "address_families": ["ipv4-unicast", "ipv6-unicast"],
              "max_prefixes": 10000,
              "import_filter": {"mode": "accept_all"} | {"mode": "scope", ...},
              "md5_password": "secret" | null,
              "md5_password_set": true
            },
            ...
          ]
        }

    There is no daemon-wide "global AS" field anywhere in the data model
    — each peer declares its own required ``local_asn`` (a collector could
    in principle peer as different local ASNs to different neighbors).
    GoBGP itself requires exactly one ``global.config.as``, so we pick the
    first enabled peer's ``local_asn`` as the daemon-wide value and render
    a per-neighbor ``local-as`` override (see :func:`_render_neighbor`) for
    any peer whose own ``local_asn`` differs — falling back to a fixed
    placeholder when there are no peers yet (matches the baked idle image
    config so a freshly-emptied bundle doesn't shift anything).

    Peers missing required fields are skipped (logged, never a hard
    failure) — a single malformed peer row must not take the whole
    collector offline.

    ``import_filter.mode == "scope"`` (restrict the reported RIB to
    specific IPAM-scoped prefixes) is enforced AGENT-SIDE, in
    :mod:`spatium_lg_agent.rib` — NOT via a rendered GoBGP import policy.
    Two empirically-verified facts against gobgpd v4.5.0 force this:
    (1) gobgpd does not load ``defined-sets`` / ``policy-definitions`` from
    the static config file at all (``gobgp policy prefix`` reports "Nothing
    defined yet" and a referenced-but-unloaded import policy silently
    falls back to accept-all — the exact "silently ignored" failure mode
    this would be meant to fix); and (2) the collector reads each peer's
    **Adj-RIB-In** (the raw received table, pre-import-policy), so a GoBGP
    import policy — which only filters the post-selection Loc-RIB — could
    never change what the collector observes anyway. So the scope is
    applied where it actually works: ``rib.py`` filters each peer's parsed
    adj-in routes against the scope prefix set before pushing. See
    ``gobgp.peer_import_scopes`` + ``RibPoller._poll_adj_in``. The
    receive-only invariant is orthogonal and untouched (import-side
    filtering only narrows what's reported; it can never cause an export).
    """
    peers_in = [p for p in (bundle.get("peers") or []) if p.get("enabled", True)]
    global_as = _PLACEHOLDER_AS
    for p in peers_in:
        local_asn = p.get("local_asn")
        if local_asn:
            try:
                global_as = int(local_asn)
                break
            except (TypeError, ValueError):
                continue
    router_id = _derive_router_id()

    neighbors: list[dict[str, Any]] = []
    for peer in peers_in:
        try:
            neighbors.append(_render_neighbor(peer, global_as))
        except (KeyError, ValueError, TypeError) as e:
            log.warning(
                "lg_peer_render_skipped", peer=peer.get("peer_id"), error=str(e)
            )

    rendered: dict[str, Any] = {
        "global": {
            "config": {
                "as": global_as,
                "router-id": router_id,
                "port": _DEFAULT_BGP_PORT,
            },
            # ── THE receive-only enforcement point — see module docstring.
            "apply-policy": {
                "config": {
                    "default-import-policy": "accept-route",
                    "default-export-policy": "reject-route",
                }
            },
        },
        "neighbors": neighbors,
    }
    _assert_receive_only(rendered)
    return rendered


def _render_neighbor(peer: dict[str, Any], global_as: int) -> dict[str, Any]:
    address = str(peer["peer_address"])
    peer_asn = int(peer["peer_asn"])
    peer_id = str(peer["peer_id"])

    # NOTE: import_filter.mode == "scope" is intentionally NOT rendered
    # into a GoBGP import policy here — it is enforced agent-side in
    # rib.py (see render_config's docstring for the empirical reason).

    conf: dict[str, Any] = {
        "neighbor-address": address,
        "peer-as": peer_asn,
    }
    local_asn = peer.get("local_asn")
    if local_asn and int(local_asn) != global_as:
        conf["local-as"] = int(local_asn)
    md5 = peer.get("md5_password")
    if md5:
        conf["auth-password"] = str(md5)

    afi_safis: list[dict[str, Any]] = []
    try:
        max_prefixes = int(peer.get("max_prefixes") or _DEFAULT_MAX_PREFIXES)
    except (TypeError, ValueError):
        max_prefixes = _DEFAULT_MAX_PREFIXES
    families = peer.get("address_families") or ["ipv4-unicast"]
    for fam in families:
        if fam not in _KNOWN_AFI_SAFIS:
            log.warning("lg_peer_unknown_afi_safi", peer=peer_id, family=fam)
            continue
        afi_safis.append(
            {
                "config": {"afi-safi-name": fam},
                # D4 (max-prefix cap) — MUST be rendered here, not just
                # stored in the DB. See module docstring for the
                # empirical verification of this behavior.
                "prefix-limit": {
                    "config": {
                        "max-prefixes": max_prefixes,
                        "shutdown-threshold-pct": _SHUTDOWN_THRESHOLD_PCT,
                    }
                },
            }
        )
    if not afi_safis:
        raise ValueError(f"peer {peer_id} has no usable address families")

    neighbor: dict[str, Any] = {
        "config": conf,
        "afi-safis": afi_safis,
        # Defense-in-depth only — NOT the enforcement point (see the
        # module docstring). gobgpd's static config loader does not
        # currently wire a per-neighbor apply-policy block into a real
        # policy assignment for a normal peer, but setting it defensively
        # costs nothing and protects against a future gobgpd version that
        # DOES start honoring it per-peer.
        "apply-policy": {
            "config": {
                "default-import-policy": "accept-route",
                "default-export-policy": "reject-route",
            }
        },
    }
    return neighbor


def _assert_receive_only(rendered: dict[str, Any]) -> None:
    """Hard fail-safe — never a soft warning.

    Every rendered config, no matter how peers were constructed above,
    must satisfy this before it is ever written to disk or handed to
    gobgpd. This is the review gate the module docstring refers to.
    """
    g = rendered.get("global", {})
    default_export = (
        g.get("apply-policy", {}).get("config", {}).get("default-export-policy")
    )
    if default_export != "reject-route":
        raise ReceiveOnlyViolation(
            "global.apply-policy.config.default-export-policy must be "
            f"'reject-route', got {default_export!r}"
        )
    for n in rendered.get("neighbors", []):
        conf = n.get("config", {})
        if "export-policy-list" in n.get("apply-policy", {}).get("config", {}):
            raise ReceiveOnlyViolation(
                f"neighbor {conf.get('neighbor-address')} carries an "
                "export-policy-list — the receive-only collector must "
                "never define one"
            )
        if n.get("route-server", {}).get("config", {}).get("route-server-client"):
            raise ReceiveOnlyViolation(
                f"neighbor {conf.get('neighbor-address')} has "
                "route-server-client set — never valid for this collector"
            )


def write_config(cfg: AgentConfig, bundle: dict[str, Any]) -> dict[str, Any]:
    """Render + atomically write the gobgpd config file.

    Also stashes a copy under the agent state dir's ``rendered/`` for
    audit/debug (mirrors the DHCP agent's ``rendered/kea-dhcp4.json``
    convention).
    """
    rendered = render_config(bundle)
    target = cfg.gobgpd_config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(rendered, indent=2, sort_keys=True))
    tmp.replace(target)
    save_rendered_gobgpd(cfg.state_dir, rendered)
    return rendered


def start_daemon(cfg: AgentConfig) -> subprocess.Popen[bytes]:
    """Launch gobgpd as a tracked child process (mirrors the DNS agent's
    ``Bind9Driver.start_daemon`` — the Python agent owns the daemon
    subprocess directly rather than a shell-level supervise loop, since
    there is exactly one daemon to manage here, same as BIND9's single
    ``named``).

    The image ships a valid "idle" config at ``cfg.gobgpd_config_path``
    (empty neighbor list, receive-only global policy already set) so this
    always has something to boot from — the first successful
    :func:`apply_config` (from cache, or from the control plane) rewrites
    it in place and reloads.
    """
    proc = subprocess.Popen(
        [
            cfg.gobgpd_bin,
            "-f",
            str(cfg.gobgpd_config_path),
            "--api-hosts",
            f"{cfg.gobgp_grpc_host}:{cfg.gobgp_grpc_port}",
            *_GOBGPD_ARGS,
        ]
    )
    log.info("gobgpd_started", pid=proc.pid)
    return proc


def wait_until_ready(
    cfg: AgentConfig,
    proc: subprocess.Popen[bytes] | None = None,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> bool:
    """Block until gobgpd's gRPC API answers a real request.

    Gates the sync thread's first ``apply_config`` (→ :func:`reload` → SIGHUP)
    on gobgpd being fully up. gobgpd installs its ``signal.Notify(SIGHUP)``
    reload handler partway through startup; a SIGHUP that lands *before* then
    hits the default disposition — **terminate** — and kills the daemon. The
    bootstrap-from-cache apply fires within a few ms of :func:`start_daemon`,
    so on a slow host that SIGHUP beats the handler and gobgpd dies with no
    output ~1s in (issue #576 — reproduced on the k3s appliance; Docker won the
    race locally, which is why this only surfaced in the field). A successful
    ``gobgp neighbor`` call proves the gRPC server is serving requests, by which
    point the signal handler is installed and any SIGHUP is safe.

    Returns ``True`` once ready. Returns ``False`` if gobgpd exits during
    startup (a genuine config/bind failure — surfaced immediately) or the
    timeout elapses; the caller proceeds regardless — ``--config-auto-reload``
    still delivers the config via gobgpd's own file watch, and the supervisor's
    ``daemon_running`` check catches a truly dead daemon on its next tick.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            log.error("gobgpd_exited_during_startup", returncode=proc.returncode)
            return False
        try:
            run_gobgp_cli(cfg, "neighbor", timeout=5.0)
            log.info("gobgpd_ready")
            return True
        except GoBGPCliError:
            time.sleep(interval)
    log.warning("gobgpd_ready_timeout", timeout=timeout)
    return False


def daemon_running(proc: subprocess.Popen[bytes] | None) -> bool:
    return proc is not None and proc.poll() is None


def reload(proc: subprocess.Popen[bytes] | None) -> None:
    """Signal a running gobgpd to re-read its config file.

    gobgpd's own SIGHUP handler (``cmd/gobgpd/main.go``) calls
    ``config.ReadConfigFile`` + ``UpdateConfig`` on receipt — this is
    upstream's own documented reload mechanism, not a SpatiumDDI
    invention, and (unlike an unhandled signal) does NOT terminate the
    process. Best-effort: if the process is gone, the main supervisor
    loop's ``daemon_running`` check will notice and exit(2) so the
    container restarts — this function never raises.
    """
    if proc is None or proc.poll() is not None:
        log.warning("gobgpd_reload_not_running")
        return
    try:
        os.kill(proc.pid, signal.SIGHUP)
        log.info("gobgpd_reload_sighup_sent", pid=proc.pid)
    except ProcessLookupError:
        log.warning("gobgpd_reload_pid_not_running", pid=proc.pid)


def apply_config(
    cfg: AgentConfig, bundle: dict[str, Any], proc: subprocess.Popen[bytes] | None
) -> dict[str, Any]:
    """Render + write + reload in one call.

    This is what ``SyncLoop`` invokes on every bundle change, and once at
    startup from the on-disk cache (non-negotiable #5 — the collector's
    BGP sessions stay up from cache if the control plane is unreachable).
    ``proc`` may be ``None`` when applying the cache before gobgpd has
    even been started yet (the config file write still happens — the
    daemon picks it up on its own first read).
    """
    rendered = write_config(cfg, bundle)
    if proc is not None:
        reload(proc)
    return rendered


def peer_address_map(bundle: dict[str, Any]) -> dict[str, str]:
    """``peer_address -> peer_id`` map used by :mod:`spatium_lg_agent.rib`
    to know which ``bgp_lg_peer`` row (``peer_id`` in the wire bundle —
    see ``LGPeerDef.peer_id``) each neighbor's Adj-RIB-In belongs to when
    it polls per-neighbor ``gobgp neighbor <peer_address> adj-in``."""
    out: dict[str, str] = {}
    for peer in bundle.get("peers") or []:
        addr = peer.get("peer_address")
        pid = peer.get("peer_id")
        if addr and pid:
            out[str(addr)] = str(pid)
    return out


def peer_import_scopes(bundle: dict[str, Any]) -> dict[str, list[_IPNetwork]]:
    """``peer_id -> [parsed scope networks]`` for every scope-mode peer.

    Used by :mod:`spatium_lg_agent.rib` to filter each peer's Adj-RIB-In
    down to the scoped prefixes before pushing (see ``render_config``'s
    docstring for why the scope is enforced agent-side rather than as a
    GoBGP import policy). ``accept_all`` peers are ABSENT from the map —
    ``rib.py`` treats "peer_id not in map" as "no filter, report
    everything". A scope-mode peer with an empty/all-invalid prefix list
    maps to ``[]``, which means "report nothing" (the literal meaning of
    restricting to zero prefixes) — logged so the operator can see why a
    scoped peer is reporting no routes."""
    out: dict[str, list[_IPNetwork]] = {}
    for peer in bundle.get("peers") or []:
        if not peer.get("enabled", True):
            continue
        pid = peer.get("peer_id")
        imp = peer.get("import_filter") or {}
        if not pid or imp.get("mode") != "scope":
            continue
        nets: list[_IPNetwork] = []
        for p in imp.get("prefixes") or []:
            try:
                nets.append(ipaddress.ip_network(str(p), strict=False))
            except ValueError:
                log.warning("lg_scope_prefix_invalid", peer=str(pid), prefix=p)
        if not nets:
            log.warning("lg_scope_empty", peer=str(pid))
        out[str(pid)] = nets
    return out


# Serializes CLI invocations so two threads (e.g. a slow rib poll + an
# operator-triggered debug call) don't interleave subprocess launches
# unnecessarily. gobgpd's gRPC server itself is safe for concurrent
# access; this lock is just to keep our own subprocess bookkeeping simple.
_cli_lock = threading.Lock()


def run_gobgp_cli(cfg: AgentConfig, *args: str, timeout: float = 15.0) -> str:
    """Shell out to the ``gobgp`` CLI against the local gobgpd gRPC listener.

    Uses ``-u <host> -p <port>`` (both loopback-only by default — see the
    image's ``--api-hosts=127.0.0.1:50051``). Returns raw stdout; raises
    :class:`GoBGPCliError` on a non-zero exit or if the binary/daemon
    can't be reached.
    """
    cmd = [
        cfg.gobgp_bin,
        "-u",
        cfg.gobgp_grpc_host,
        "-p",
        str(cfg.gobgp_grpc_port),
        *args,
    ]
    with _cli_lock:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GoBGPCliError(f"failed to run {' '.join(cmd)}: {e}") from e
    if result.returncode != 0:
        raise GoBGPCliError(
            f"{' '.join(cmd)} exited {result.returncode}: "
            f"{result.stderr.strip()[:400]}"
        )
    return result.stdout
