"""Tests for #272 — supervisor heartbeat-target resolution.

A supervisor that is itself part of the control-plane k3s cluster
should heartbeat the in-cluster api Service (resilient to any single
node loss + kube-proxy load-balanced) rather than the seed node's IP
that got baked into ``CONTROL_PLANE_URL`` at install/join time. Remote
(off-cluster) DNS/DHCP agents have no in-cluster DNS to resolve the
``.svc`` name, so they keep using their configured URL — ideally the
MetalLB VIP.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_supervisor import appliance_state, heartbeat
from spatium_supervisor.config import SupervisorConfig


def _cfg(url: str, tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        control_plane_url=url,
        hostname="test-node",
        state_dir=tmp_path,
        bootstrap_pairing_code="",
        heartbeat_interval_seconds=30,
        k8s_proxy_enabled=False,
        in_pod_firewall_enabled=False,
    )


def test_control_plane_seed_targets_in_cluster_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A control-plane seed redirects to the in-cluster Service even
    # though CONTROL_PLANE_URL points at its own external IP.
    monkeypatch.setattr(
        appliance_state, "detect_appliance_variant", lambda: "control-plane"
    )
    monkeypatch.setattr(
        appliance_state, "read_cluster_join_state", lambda: (None, None)
    )
    cfg = _cfg("https://192.168.0.199", tmp_path)
    assert (
        heartbeat._effective_control_plane_url(cfg)
        == heartbeat._IN_CLUSTER_CONTROL_PLANE_URL
    )


def test_promoted_appliance_targets_in_cluster_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An appliance promoted into the control plane (cluster_join_state
    # ready) heartbeats the cluster, not the old seed IP it joined via.
    monkeypatch.setattr(
        appliance_state, "detect_appliance_variant", lambda: "appliance"
    )
    monkeypatch.setattr(
        appliance_state, "read_cluster_join_state", lambda: ("ready", None)
    )
    cfg = _cfg("https://192.168.0.199", tmp_path)
    assert (
        heartbeat._effective_control_plane_url(cfg)
        == heartbeat._IN_CLUSTER_CONTROL_PLANE_URL
    )


def test_remote_agent_keeps_configured_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A plain data-plane appliance that hasn't joined the control plane
    # uses its configured URL (which an operator should point at the VIP).
    monkeypatch.setattr(
        appliance_state, "detect_appliance_variant", lambda: "appliance"
    )
    monkeypatch.setattr(
        appliance_state, "read_cluster_join_state", lambda: (None, None)
    )
    cfg = _cfg("https://192.168.0.252", tmp_path)
    assert heartbeat._effective_control_plane_url(cfg) == "https://192.168.0.252"


def test_join_state_not_ready_keeps_configured_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Mid-promotion (join state pending/failed) is NOT a cluster member
    # yet — keep heartbeating the configured URL until the join settles.
    monkeypatch.setattr(
        appliance_state, "detect_appliance_variant", lambda: "appliance"
    )
    monkeypatch.setattr(
        appliance_state, "read_cluster_join_state", lambda: ("joining", None)
    )
    cfg = _cfg("https://192.168.0.199", tmp_path)
    assert heartbeat._effective_control_plane_url(cfg) == "https://192.168.0.199"
