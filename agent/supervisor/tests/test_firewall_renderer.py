"""Tests for the supervisor nftables drop-in renderer.

Covers #272 Phase 7b + #285 Phase 1 — the renderer emits the full
authoritative scoped rule set: a data-plane floor (flannel/wireguard),
per-role service ports, peer-scoped etcd/kubelet (NEVER LAN-wide),
6443 widened to peers ∪ pod ∪ service ∪ kubeapi_expose, and MetalLB
memberlist gated on a multi-node + VIP cluster — all family-split
(``ip saddr`` v4 / ``ip6 saddr`` v6).

These rules are ADDITIVE on the current baked base conf; they only
become authoritative once #285 Phase 1b removes the LAN-wide base accept.
"""

from __future__ import annotations

from spatium_supervisor.firewall_renderer import render_drop_in

_PEERS_V4 = ["192.168.0.133", "192.168.0.125/32"]


# ── Management floor (always) ────────────────────────────────────────


def test_idle_renders_management_floor_only() -> None:
    p = render_drop_in({"roles": []})
    assert 'tcp dport 22 accept comment "ssh"' in p.body
    assert "icmp type echo-request accept" in p.body
    assert "icmpv6 type echo-request accept" in p.body
    assert "iif lo accept" in p.body
    # No CP / data-plane / role rules.
    assert "k3s-peer" not in p.body
    assert "kubeapi" not in p.body
    assert "dataplane" not in p.body
    assert "role:" not in p.body


# ── Per-role service ports ───────────────────────────────────────────


def test_role_ports() -> None:
    p = render_drop_in({"roles": ["dns-bind9", "dhcp"]})
    assert 'tcp dport 53 accept comment "role:dns-and-dhcp"' in p.body
    assert 'udp dport 53 accept comment "role:dns-and-dhcp"' in p.body
    assert "udp dport 67 accept" in p.body
    assert "udp dport 68 accept" in p.body
    assert 53 in p.expected_tcp_ports and 67 in p.expected_udp_ports


# ── Control-plane peer scoping (the #285 hardening) ──────────────────


def test_peers_scope_etcd_kubelet_not_lanwide() -> None:
    p = render_drop_in({"roles": []}, _PEERS_V4)
    # etcd + kubelet bundled into ONE peer-scoped rule, never bare.
    assert (
        "ip saddr { 192.168.0.125/32, 192.168.0.133/32 } tcp dport { 2379, 2380, 10250 } accept"
        in (p.body)
    )
    # Crucially: no UNSCOPED accept for the sensitive ports.
    assert "tcp dport 2379 accept comment" not in p.body
    assert 'tcp dport { 2379, 2380, 10250 } accept comment "k3s-peer-v4"' in p.body
    for port in (2379, 2380, 10250, 6443):
        assert port in p.expected_tcp_ports


def test_6443_widens_to_peers_pod_service_kubeapi() -> None:
    p = render_drop_in(
        {"roles": [], "kubeapi_expose_cidrs": ["10.9.0.0/24"]},
        _PEERS_V4,
        pod_cidrs=["10.42.0.0/16"],
        service_cidrs=["10.43.0.0/16"],
    )
    # All four sources land in the 6443 saddr set, sorted + deduped.
    line = next(ln for ln in p.body.splitlines() if "kubeapi-v4" in ln and "dport 6443" in ln)
    for cidr in ("10.42.0.0/16", "10.43.0.0/16", "10.9.0.0/24", "192.168.0.133/32"):
        assert cidr in line


def test_single_node_cp_opens_6443_to_pod_only_no_etcd() -> None:
    # No peers (single-node), but it runs the apiserver → 6443 to pod/svc,
    # and NO etcd/kubelet rule (single-node etcd is loopback).
    p = render_drop_in(
        {"roles": []}, [], pod_cidrs=["10.42.0.0/16"], service_cidrs=["10.43.0.0/16"]
    )
    assert "dport 6443 accept" in p.body
    assert "10.42.0.0/16" in p.body
    assert "k3s-peer" not in p.body  # no etcd/kubelet without peers


# ── Data-plane floor ─────────────────────────────────────────────────


def test_dataplane_vxlan() -> None:
    p = render_drop_in(
        {"roles": []}, _PEERS_V4, dataplane_backend="vxlan", dataplane_peer_cidrs=_PEERS_V4
    )
    assert "ip saddr { 192.168.0.125/32, 192.168.0.133/32 } udp dport 8472 accept" in p.body
    assert 8472 in p.expected_udp_ports


def test_dataplane_wireguard() -> None:
    p = render_drop_in(
        {"roles": []},
        _PEERS_V4,
        dataplane_backend="wireguard-native",
        dataplane_peer_cidrs=_PEERS_V4,
    )
    assert "udp dport { 51820, 51821 } accept" in p.body
    assert 51820 in p.expected_udp_ports and 51821 in p.expected_udp_ports


def test_dataplane_hostgw_or_unknown_opens_nothing() -> None:
    for backend in ("host-gw", "none", "weird-backend", ""):
        p = render_drop_in(
            {"roles": []}, _PEERS_V4, dataplane_backend=backend, dataplane_peer_cidrs=_PEERS_V4
        )
        assert "dataplane" not in p.body
        assert 8472 not in p.expected_udp_ports


# ── IPv6 family split (the v6 lockout fix) ───────────────────────────


def test_v6_peers_use_ip6_saddr_and_128() -> None:
    p = render_drop_in(
        {"roles": []},
        ["192.168.0.10", "2001:db8::10", "2001:db8::11/128"],
        pod_cidrs=["10.42.0.0/16", "2001:cafe:42::/56"],
        dataplane_backend="vxlan",
        dataplane_peer_cidrs=["192.168.0.10", "2001:db8::10"],
    )
    # v6 peers render as ip6 saddr with /128, never folded into a v4 set.
    assert "ip6 saddr { 2001:db8::10/128, 2001:db8::11/128 } tcp dport { 2379, 2380, 10250 }" in (
        p.body
    )
    assert "ip saddr { 192.168.0.10/32 } tcp dport { 2379, 2380, 10250 }" in p.body
    # v6 data-plane on ip6 saddr.
    assert "ip6 saddr { 2001:db8::10/128 } udp dport 8472 accept" in p.body
    # v6 pod CIDR lands in the v6 6443 set.
    assert "2001:cafe:42::/56" in next(ln for ln in p.body.splitlines() if "kubeapi-v6" in ln)


# ── MetalLB memberlist gating ────────────────────────────────────────


def test_memberlist_only_when_multinode_and_vip() -> None:
    # Single member or no VIP → no memberlist.
    assert (
        "memberlist"
        not in render_drop_in({"roles": []}, _PEERS_V4, cp_member_count=1, vip_configured=True).body
    )
    assert (
        "memberlist"
        not in render_drop_in(
            {"roles": []}, _PEERS_V4, cp_member_count=3, vip_configured=False
        ).body
    )
    # Multi-node + VIP → memberlist 7946 tcp AND udp, peer-scoped.
    p = render_drop_in({"roles": []}, _PEERS_V4, cp_member_count=3, vip_configured=True)
    assert "tcp dport 7946 accept" in p.body
    assert "udp dport 7946 accept" in p.body
    assert 7946 in p.expected_tcp_ports and 7946 in p.expected_udp_ports


# ── Injection safety + operator override ─────────────────────────────


def test_injection_rejected() -> None:
    p = render_drop_in({"roles": []}, ["1.2.3.4 }, drop; tcp dport 22 accept; #"])
    assert "drop;" not in p.body
    assert "k3s-peer" not in p.body  # the only "peer" was the injection attempt


def test_firewall_extra_appended_last() -> None:
    p = render_drop_in({"roles": ["dhcp"], "firewall_extra": 'udp dport 161 accept comment "snmp"'})
    assert p.body.rstrip().endswith('udp dport 161 accept comment "snmp"')


def test_deterministic() -> None:
    kw = dict(
        pod_cidrs=["10.42.0.0/16"],
        dataplane_backend="vxlan",
        dataplane_peer_cidrs=_PEERS_V4,
        cp_member_count=3,
        vip_configured=True,
    )
    assert (
        render_drop_in({"roles": ["dns-bind9"]}, _PEERS_V4, **kw).body
        == render_drop_in({"roles": ["dns-bind9"]}, _PEERS_V4, **kw).body
    )
