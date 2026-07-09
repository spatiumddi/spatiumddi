"""etcd / control-plane peer-drift cross-check (#285 Phase 5, warn-only).

The firewall opens etcd (2379/2380) + kubelet (10250) + memberlist (7946) to a
**peer CIDR set** the control plane derives from the known appliance rows. That
set can drift from the *live* cluster membership: a node that joined but whose
peer entry hasn't propagated (uncovered), or a left/dead member whose /32 is
still in the scope (stale). Neither breaks anything — the peer set only ever
GROWS access to real cluster members — but surfacing the drift helps operators
notice a wedged peer-resolve or a half-finished demote.

This is **warn-only**: it logs ``supervisor.firewall.peer_drift`` and never
mutates the ruleset. The live membership is read from the kube API (`GET
/api/v1/nodes`, control-plane-labelled), so it's effectively seed-only — a
non-seed node has no admin kubeconfig and the read fails best-effort to a no-op.
``compute_peer_drift`` is the pure, unit-tested core.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import structlog

from . import k8s_api

log = structlog.get_logger(__name__)

_CP_LABEL = "node-role.kubernetes.io/control-plane"

# #593 — k3s stamps this on every embedded-etcd server node. It is managed by
# k3s from ACTUAL membership, not by us: service_lifecycle.reconcile_node_labels
# only ever touches ``spatium.io/role-*``. That independence is the whole point
# — it is a statement about reality, not about the control plane's row.
_ETCD_LABEL = "node-role.kubernetes.io/etcd"

# etcd's raft peer port. Its presence in an accept rule is what distinguishes a
# drop-in that keeps this node in the cluster from one that partitions it.
_ETCD_PEER_PORT = "2380"
# Whole-number match so "12380" can't pass for "2380".
_ETCD_PEER_PORT_RE = re.compile(rf"(?<!\d){_ETCD_PEER_PORT}(?!\d)")
# nftables' own comment clause — not a "#" comment, and it survives naive
# stripping. ``comment "ssh; not 2380"`` must not read as an etcd accept.
_NFT_COMMENT_RE = re.compile(r'comment\s+"[^"]*"')


def compute_peer_drift(
    member_ips: list[str], peer_cidrs: list[str], self_ips: set[str]
) -> dict[str, list[str]]:
    """Pure drift computation.

    * ``member_ips`` — live control-plane node InternalIPs.
    * ``peer_cidrs`` — the firewall peer set we render.
    * ``self_ips`` — this node's own IPs (excluded; peers are the OTHERS).

    Returns ``{"uncovered_members": [...], "stale_cidrs": [...]}``:
    * uncovered — a live member IP (not self) not inside any peer CIDR.
    * stale — a host-route peer CIDR (/32 or /128) whose address is neither a
      live member nor self (a left/dead member still in scope). Broader CIDRs
      are skipped (can't attribute them to a single absent member).
    """

    def _addrs(values: Any) -> set[Any]:
        out: set[Any] = set()
        for v in values:
            try:
                out.add(ipaddress.ip_address(v))
            except ValueError:
                continue
        return out

    self_addrs = _addrs(self_ips)
    member_addrs = _addrs(member_ips)
    nets: list[tuple[str, Any]] = []
    for c in peer_cidrs:
        try:
            nets.append((c, ipaddress.ip_network(c, strict=False)))
        except ValueError:
            continue

    uncovered = sorted(
        str(a)
        for a in member_addrs
        if a not in self_addrs and not any(a in n for _, n in nets)
    )
    stale = sorted(
        c
        for c, n in nets
        if n.num_addresses == 1
        and n.network_address not in member_addrs
        and n.network_address not in self_addrs
    )
    return {"uncovered_members": uncovered, "stale_cidrs": stale}


def list_control_plane_node_ips() -> list[str] | None:
    """Live control-plane node InternalIPs via the kube API, or None when the
    API is unreachable (non-seed node / kubeapi down) — best-effort."""
    try:
        status, body = k8s_api._request("GET", "/api/v1/nodes")
    except RuntimeError:
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    ips: list[str] = []
    for item in data.get("items", []):
        labels = (item.get("metadata") or {}).get("labels") or {}
        if _CP_LABEL not in labels:
            continue
        for addr in (item.get("status") or {}).get("addresses") or []:
            if addr.get("type") == "InternalIP" and addr.get("address"):
                ips.append(str(addr["address"]))
    return ips


# ── #593 — never let the firewall partition a live etcd member ──────────────
#
# The per-role drop-in used to be rendered purely from the control plane's row
# (``cluster_role``). Observed live on a 3-node appliance: ddi2's row had gone
# ``cluster_role = NULL`` after a failed re-join, while ddi2 was still a voting
# etcd member. The supervisor concluded "plain agent node", rendered an agent
# firewall with no ``k3s-peer`` rule, and ddi2's own nftables silently dropped
# its peers' inbound raft traffic. Its peers logged ``dial tcp …:2380: i/o
# timeout`` every 5 s against a member that was up the whole time.
#
# The triggering row bug is fixed (#591), but the COUPLING is the hazard: any
# row/reality divergence reproduces this — a stuck heartbeat, a control plane
# restored from an older backup, a half-landed promote, an operator clearing
# state with the #591 escape hatch. And it fails in the worst direction:
# closing the peer port on a live member drops it out of raft, which on a
# 3-node cluster leaves you one node from losing quorum.
#
# So the peer rule is keyed off OBSERVED LOCAL STATE, with two defences:
#   1. recover a peer set from live membership when the row supplies none
#   2. refuse to apply any body that would close 2380 on a node that k3s says
#      is an etcd member — better a stale-but-open ruleset than a self-inflicted
#      partition (and non-negotiable #5: keep serving when the control plane is
#      wrong or unreachable).


def _etcd_membership_uncached() -> bool | None:
    node_name = os.environ.get("NODE_NAME") or os.environ.get("APPLIANCE_HOSTNAME")
    if not node_name:
        return None
    try:
        status, body = k8s_api._request("GET", f"/api/v1/nodes/{quote(node_name)}")
    except (RuntimeError, OSError):
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    labels = (data.get("metadata") or {}).get("labels") or {}
    return _ETCD_LABEL in labels


# Membership changes only on promote/demote, but the firewall path runs on
# every heartbeat — cache so we don't GET the node object each tick. The TTL
# is safe in one direction only, and that's the direction we want: a stale
# ``True`` briefly DELAYS narrowing the firewall after a real demote (harmless
# — the peer rule only ever over-permits to real cluster members), while a
# stale ``False`` would be the #593 bug itself. Never cache ``None``: an
# unreachable apiserver must be re-probed, not remembered as "don't know".
_ETCD_MEMBER_TTL_S = 30.0
_etcd_member_cache: tuple[float, bool] | None = None


def local_node_is_etcd_member() -> bool | None:
    """Does k3s consider THIS node an embedded-etcd server?

    Reads the local node's own labels. ``None`` when the answer is unknowable
    (no NODE_NAME, kube API unreachable, non-k3s) — callers must treat None as
    "don't know", never as "no", or they reintroduce the bug on any node whose
    apiserver is briefly unreachable.

    Survives the partition it guards against: on a control-plane node the kube
    API being read is served by the local apiserver.
    """
    global _etcd_member_cache
    now = time.monotonic()
    if _etcd_member_cache is not None and now - _etcd_member_cache[0] < _ETCD_MEMBER_TTL_S:
        return _etcd_member_cache[1]
    answer = _etcd_membership_uncached()
    if answer is not None:
        _etcd_member_cache = (now, answer)
    return answer


def _reset_etcd_member_cache() -> None:
    """Test hook — the cache is process-global and would leak across cases."""
    global _etcd_member_cache
    _etcd_member_cache = None


def body_opens_etcd_peers(body: str) -> bool:
    """True when `body` has at least one accept rule covering etcd's raft peer
    port.

    Two ways to get a false positive, both of which would let the guard wave
    through a body that partitions the node — the dangerous direction:

    * ``#`` comments. The renderer's header block names 2379/2380 in prose.
    * nftables ``comment "..."`` clauses, which are NOT ``#`` comments and so
      survive naive stripping. A rule commented ``"ssh; not 2380"`` would read
      as an etcd accept.

    Both are stripped before matching, and the port is matched as a whole
    number so ``12380`` can't stand in for ``2380``.
    """
    for raw in body.splitlines():
        code = _NFT_COMMENT_RE.sub("", raw.split("#", 1)[0]).strip()
        if not code or "accept" not in code:
            continue
        if _ETCD_PEER_PORT_RE.search(code):
            return True
    return False


def observed_peer_cidrs(self_ips: Iterable[str]) -> list[str]:
    """Host CIDRs for every live control-plane node except this one, read from
    the cluster itself. Empty when membership is unreadable — the caller then
    falls through to the refuse-to-apply guard rather than guessing."""
    members = list_control_plane_node_ips()
    if not members:
        return []
    mine = set(self_ips)
    out: list[str] = []
    for ip in members:
        if ip in mine:
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        out.append(f"{ip}/{addr.max_prefixlen}")
    return sorted(set(out))


def would_self_partition(body: str) -> bool:
    """True when applying `body` would close etcd's peer port on a node that
    k3s says is a live etcd member.

    False whenever membership is unknown — an unreadable kube API must not
    block a legitimate firewall update on a plain agent node.
    """
    if local_node_is_etcd_member() is not True:
        return False
    return not body_opens_etcd_peers(body)


def warn_on_peer_drift(
    peer_cidrs: list[str], self_ips: list[str]
) -> dict[str, list[str]] | None:
    """Best-effort: read live CP members, compute drift, warn-log if any.
    Returns the drift dict (for tests) or None when membership is unreadable."""
    members = list_control_plane_node_ips()
    if members is None:
        return None
    drift = compute_peer_drift(members, peer_cidrs, set(self_ips))
    if drift["uncovered_members"] or drift["stale_cidrs"]:
        log.warning(
            "supervisor.firewall.peer_drift",
            uncovered_members=drift["uncovered_members"],
            stale_cidrs=drift["stale_cidrs"],
            peer_cidrs=peer_cidrs,
        )
    return drift
