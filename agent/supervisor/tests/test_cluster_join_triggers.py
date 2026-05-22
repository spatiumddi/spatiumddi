"""Supervisor control-plane join/leave trigger writers (#272 Phase 7b).

Covers the appliance-only gate, the trigger payload shape, idempotency
(no stacking while a trigger is unconsumed), and the .state / token
sidecar readers. The actual k3s reconfigure is the host-side runner
(spatium-cluster-join) which needs a real multi-VM cluster to validate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def appliance_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every cluster trigger/sidecar path at a tmp dir + force the
    appliance deployment kind so the gates pass."""
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(
        appliance_state, "_CLUSTER_JOIN_TRIGGER_FILE", tmp_path / "cluster-join-pending"
    )
    monkeypatch.setattr(
        appliance_state, "_CLUSTER_LEAVE_TRIGGER_FILE", tmp_path / "cluster-leave-pending"
    )
    monkeypatch.setattr(
        appliance_state, "_CLUSTER_JOIN_STATE_SIDECAR", tmp_path / "cluster-join.state"
    )
    monkeypatch.setattr(appliance_state, "_K3S_JOIN_TOKEN_SIDECAR", tmp_path / "k3s-join-token")
    return tmp_path


def test_join_writes_trigger_with_payload(appliance_paths: Path) -> None:
    fired = appliance_state.maybe_fire_cluster_join("member", "https://10.0.0.1:6443", "K10::tok")
    assert fired is True
    body = (appliance_paths / "cluster-join-pending").read_text()
    assert body == "https://10.0.0.1:6443\nK10::tok\n"


def test_join_idempotent_until_consumed(appliance_paths: Path) -> None:
    assert appliance_state.maybe_fire_cluster_join("member", "https://s:6443", "t") is True
    # Trigger already present → don't stack.
    assert appliance_state.maybe_fire_cluster_join("member", "https://s:6443", "t") is False


def test_join_requires_member_role_and_coordinates(appliance_paths: Path) -> None:
    assert appliance_state.maybe_fire_cluster_join("none", "https://s:6443", "t") is False
    assert appliance_state.maybe_fire_cluster_join("member", None, "t") is False
    assert appliance_state.maybe_fire_cluster_join("member", "https://s:6443", None) is False
    assert not (appliance_paths / "cluster-join-pending").exists()


def test_join_skipped_off_appliance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    monkeypatch.setattr(
        appliance_state, "_CLUSTER_JOIN_TRIGGER_FILE", tmp_path / "cluster-join-pending"
    )
    assert appliance_state.maybe_fire_cluster_join("member", "https://s:6443", "t") is False
    assert not (tmp_path / "cluster-join-pending").exists()


def test_leave_writes_trigger(appliance_paths: Path) -> None:
    assert appliance_state.maybe_fire_cluster_leave("none") is True
    assert (appliance_paths / "cluster-leave-pending").exists()


def test_leave_requires_none_role(appliance_paths: Path) -> None:
    assert appliance_state.maybe_fire_cluster_leave("member") is False
    assert not (appliance_paths / "cluster-leave-pending").exists()


def test_read_cluster_join_state(appliance_paths: Path) -> None:
    # Sidecar shape is "state\treason"; a bare state has no reason.
    (appliance_paths / "cluster-join.state").write_text("ready\t")
    assert appliance_state.read_cluster_join_state() == ("ready", None)
    (appliance_paths / "cluster-join.state").write_text("failed\tetcd join refused")
    assert appliance_state.read_cluster_join_state() == ("failed", "etcd join refused")


def test_read_cluster_join_state_missing(appliance_paths: Path) -> None:
    assert appliance_state.read_cluster_join_state() == (None, None)


def test_read_k3s_join_token(appliance_paths: Path) -> None:
    assert appliance_state.read_k3s_join_token() is None
    (appliance_paths / "k3s-join-token").write_text("K10::servertoken\n")
    assert appliance_state.read_k3s_join_token() == "K10::servertoken"
