"""etcd/peer-drift cross-check (#285 Phase 5) — pure compute_peer_drift."""

from __future__ import annotations

from spatium_supervisor.firewall_peer_audit import compute_peer_drift


def test_no_drift_when_all_members_covered() -> None:
    d = compute_peer_drift(
        member_ips=["192.168.0.10", "192.168.0.11", "192.168.0.12"],
        peer_cidrs=["192.168.0.11/32", "192.168.0.12/32"],  # peers = the OTHERS
        self_ips={"192.168.0.10"},
    )
    assert d == {"uncovered_members": [], "stale_cidrs": []}


def test_uncovered_member_flagged() -> None:
    # .12 is a live member but the peer set doesn't include it → drift.
    d = compute_peer_drift(
        member_ips=["192.168.0.10", "192.168.0.11", "192.168.0.12"],
        peer_cidrs=["192.168.0.11/32"],
        self_ips={"192.168.0.10"},
    )
    assert d["uncovered_members"] == ["192.168.0.12"]
    assert d["stale_cidrs"] == []


def test_stale_host_route_flagged() -> None:
    # .99/32 is in the peer set but no live member nor self → left/dead member.
    d = compute_peer_drift(
        member_ips=["192.168.0.10", "192.168.0.11"],
        peer_cidrs=["192.168.0.11/32", "192.168.0.99/32"],
        self_ips={"192.168.0.10"},
    )
    assert d["uncovered_members"] == []
    assert d["stale_cidrs"] == ["192.168.0.99/32"]


def test_broad_cidr_not_flagged_stale() -> None:
    # A non-host CIDR (covers many) is never flagged stale (can't attribute).
    d = compute_peer_drift(
        member_ips=["10.0.0.5"],
        peer_cidrs=["10.0.0.0/8"],
        self_ips=set(),
    )
    assert d == {"uncovered_members": [], "stale_cidrs": []}


def test_dual_stack_and_junk_ignored() -> None:
    d = compute_peer_drift(
        member_ips=["192.168.0.11", "2001:db8::11", "not-an-ip"],
        peer_cidrs=["192.168.0.11/32", "2001:db8::11/128", "garbage"],
        self_ips=set(),
    )
    assert d == {"uncovered_members": [], "stale_cidrs": []}
