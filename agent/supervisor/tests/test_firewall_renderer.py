"""Tests for the supervisor nftables drop-in renderer.

Covers #272 Phase 7b — control-plane peer port openings. The base
appliance firewall only accepts :6443 from the pod CIDR, so the
cross-node join handshake + etcd quorum need the supervisor to render
``ip saddr { <peers> } tcp dport <port> accept`` rules for the k3s
server ports.
"""

from __future__ import annotations

from spatium_supervisor.firewall_renderer import _K3S_PEER_PORTS_TCP, render_drop_in


def test_no_peers_renders_no_k3s_rules() -> None:
    profile = render_drop_in({"roles": []})
    assert "k3s-cp-peer" not in profile.body
    for port in _K3S_PEER_PORTS_TCP:
        assert port not in profile.expected_tcp_ports


def test_peer_cidrs_open_k3s_server_ports() -> None:
    profile = render_drop_in({"roles": []}, ["192.168.0.133", "192.168.0.125/32"])
    # Every k3s server port gets a saddr-restricted accept rule.
    for port in _K3S_PEER_PORTS_TCP:
        assert f"tcp dport {port} accept" in profile.body
        assert port in profile.expected_tcp_ports
    # Bare IP is canonicalised to /32; both peers land in the saddr set.
    assert "192.168.0.133/32" in profile.body
    assert "192.168.0.125/32" in profile.body
    assert profile.body.count("k3s-cp-peer") == len(_K3S_PEER_PORTS_TCP)


def test_peer_cidrs_reject_injection() -> None:
    # An injection attempt that doesn't round-trip through ip_network is
    # dropped, leaving no k3s rules at all.
    profile = render_drop_in({"roles": []}, ["1.2.3.4 }, drop; tcp dport 22 accept; #"])
    assert "drop;" not in profile.body
    assert "k3s-cp-peer" not in profile.body
