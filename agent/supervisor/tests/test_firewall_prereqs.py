"""Supervisor fleet-firewall prerequisite collectors (#285 Phase 1).

Covers the file-parsing readers (``read_cluster_cidrs`` /
``read_base_conf_marker`` / ``_scan_flannel_backend``) against temp files,
plus ``read_node_ips`` with a stubbed kubeapi. These are purely additive
telemetry collectors — nothing here renders or applies a firewall.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from spatium_supervisor import appliance_state, k8s_api


@pytest.fixture
def _k3s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the runtime + deployment gates pass so the readers run."""
    monkeypatch.setattr(appliance_state, "detect_runtime", lambda: "k3s")
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")


# ── flannel-backend scan ─────────────────────────────────────────────


def test_scan_flannel_backend() -> None:
    assert appliance_state._scan_flannel_backend("flannel-backend: wireguard-native") == (
        "wireguard-native"
    )
    assert appliance_state._scan_flannel_backend('flannel-backend: "host-gw"\n') == "host-gw"
    assert appliance_state._scan_flannel_backend("write-kubeconfig-mode: 0644") is None
    assert appliance_state._scan_flannel_backend("") is None


# ── cluster CIDRs ────────────────────────────────────────────────────


def test_read_cluster_cidrs_dropin(monkeypatch, _k3s, tmp_path) -> None:
    dropin = tmp_path / "spatium-cidrs.yaml"
    dropin.write_text(
        "cluster-cidr: 10.42.0.0/16\nservice-cidr: 10.43.0.0/16\ncluster-dns: 10.43.0.10\n"
    )
    main = tmp_path / "config.yaml"
    main.write_text("write-kubeconfig-mode: 0644\n")  # no flannel-backend → default
    monkeypatch.setattr(appliance_state, "_K3S_CIDRS_DROPIN", dropin)
    monkeypatch.setattr(appliance_state, "_K3S_MAIN_CONFIG", main)
    monkeypatch.setattr(appliance_state, "_K3S_CONFIG_DIR", tmp_path)

    pod, svc, backend = appliance_state.read_cluster_cidrs()
    assert pod == "10.42.0.0/16"
    assert svc == "10.43.0.0/16"
    assert backend == "vxlan"  # k3s upstream default when unset


def test_read_cluster_cidrs_dualstack_and_explicit_backend(monkeypatch, _k3s, tmp_path) -> None:
    dropin = tmp_path / "spatium-cidrs.yaml"
    dropin.write_text(
        "cluster-cidr: 10.42.0.0/16,2001:cafe:42::/56\n"
        "service-cidr: 10.43.0.0/16,2001:cafe:43::/112\n"
    )
    main = tmp_path / "config.yaml"
    main.write_text("flannel-backend: wireguard-native\n")
    monkeypatch.setattr(appliance_state, "_K3S_CIDRS_DROPIN", dropin)
    monkeypatch.setattr(appliance_state, "_K3S_MAIN_CONFIG", main)
    monkeypatch.setattr(appliance_state, "_K3S_CONFIG_DIR", tmp_path)

    pod, svc, backend = appliance_state.read_cluster_cidrs()
    assert pod == "10.42.0.0/16,2001:cafe:42::/56"  # dual-stack pair preserved verbatim
    assert svc == "10.43.0.0/16,2001:cafe:43::/112"
    assert backend == "wireguard-native"


def test_read_cluster_cidrs_missing_dropin_still_defaults_backend(
    monkeypatch, _k3s, tmp_path
) -> None:
    # Pre-#302 appliance: no spatium-cidrs.yaml drop-in. pod/service CIDR
    # come back None (backend leaves columns alone) but the data-plane
    # backend still resolves to the k3s default.
    monkeypatch.setattr(appliance_state, "_K3S_CIDRS_DROPIN", tmp_path / "absent.yaml")
    monkeypatch.setattr(appliance_state, "_K3S_MAIN_CONFIG", tmp_path / "absent-config.yaml")
    monkeypatch.setattr(appliance_state, "_K3S_CONFIG_DIR", tmp_path)
    pod, svc, backend = appliance_state.read_cluster_cidrs()
    assert pod is None and svc is None
    assert backend == "vxlan"


def test_read_cluster_cidrs_off_k3s(monkeypatch) -> None:
    monkeypatch.setattr(appliance_state, "detect_runtime", lambda: "docker")
    assert appliance_state.read_cluster_cidrs() == (None, None, None)


# ── base-conf marker ─────────────────────────────────────────────────


def test_read_base_conf_marker_legacy_lanwide(monkeypatch, _k3s, tmp_path) -> None:
    conf = tmp_path / "nftables.conf"
    body = '... tcp dport { 6443, 2379, 2380, 10250 } accept comment "k3s-ha"\n'
    conf.write_text(body)
    monkeypatch.setattr(appliance_state, "_HOST_NFTABLES_CONF", conf)
    marker, lanwide = appliance_state.read_base_conf_marker()
    assert marker == hashlib.sha256(body.encode()).hexdigest()
    assert lanwide is True


def test_read_base_conf_marker_hardened(monkeypatch, _k3s, tmp_path) -> None:
    conf = tmp_path / "nftables.conf"
    conf.write_text('tcp dport 22 accept comment "ssh-floor"\n')  # no k3s-ha line
    monkeypatch.setattr(appliance_state, "_HOST_NFTABLES_CONF", conf)
    marker, lanwide = appliance_state.read_base_conf_marker()
    assert marker and len(marker) == 64
    assert lanwide is False


def test_read_base_conf_marker_unmounted(monkeypatch, _k3s, tmp_path) -> None:
    monkeypatch.setattr(appliance_state, "_HOST_NFTABLES_CONF", tmp_path / "absent.conf")
    assert appliance_state.read_base_conf_marker() == (None, None)


def test_read_base_conf_marker_off_appliance(monkeypatch) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    assert appliance_state.read_base_conf_marker() == (None, None)


# ── node_ips (all InternalIPs, both families) ────────────────────────


def test_read_node_ips_dualstack(monkeypatch, _k3s) -> None:
    monkeypatch.setenv("NODE_NAME", "node-1")
    node = {
        "status": {
            "addresses": [
                {"type": "InternalIP", "address": "192.168.1.11"},
                {"type": "InternalIP", "address": "2001:db8::11"},
                {"type": "Hostname", "address": "node-1"},
                {"type": "ExternalIP", "address": "203.0.113.5"},
            ]
        }
    }
    monkeypatch.setattr(k8s_api, "_request", lambda *a, **k: (200, json.dumps(node)))
    ips = appliance_state.read_node_ips()
    assert ips == ["192.168.1.11", "2001:db8::11"]  # only InternalIP, both families


def test_read_node_ips_none_when_empty(monkeypatch, _k3s) -> None:
    monkeypatch.setenv("NODE_NAME", "node-1")
    monkeypatch.setattr(
        k8s_api, "_request", lambda *a, **k: (200, json.dumps({"status": {"addresses": []}}))
    )
    assert appliance_state.read_node_ips() is None


def test_read_node_ips_off_k3s(monkeypatch) -> None:
    monkeypatch.setattr(appliance_state, "detect_runtime", lambda: "docker")
    assert appliance_state.read_node_ips() is None
