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
from typing import Any

import structlog

from . import k8s_api

log = structlog.get_logger(__name__)

_CP_LABEL = "node-role.kubernetes.io/control-plane"


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
