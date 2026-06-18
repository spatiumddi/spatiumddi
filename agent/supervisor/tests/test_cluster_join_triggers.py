"""Supervisor control-plane join/leave trigger writers (#272 Phase 7b).

Covers the appliance-only gate, the trigger payload shape, idempotency
(no stacking while a trigger is unconsumed), and the .state / token
sidecar readers. The actual k3s reconfigure is the host-side runner
(spatium-cluster-join) which needs a real multi-VM cluster to validate.
"""

from __future__ import annotations

import stat
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
    # Three lines: the guardrail confirm marker, then server_url + token.
    assert body == (
        f"{appliance_state._CLUSTER_JOIN_CONFIRM}\nhttps://10.0.0.1:6443\nK10::tok\n"
    )


def test_join_trigger_is_owner_only(appliance_paths: Path) -> None:
    # The payload carries the k3s join token (a control-plane-admin-
    # equivalent secret) into the 1777-sticky release-state dir — the
    # trigger file must be 0o600 so no other unprivileged host user can
    # read it before the root runner consumes it (sec scanning #82).
    assert appliance_state.maybe_fire_cluster_join("member", "https://s:6443", "tok") is True
    mode = stat.S_IMODE((appliance_paths / "cluster-join-pending").stat().st_mode)
    assert mode == 0o600


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


# ── guided etcd restore (#272 Phase 9b) ──────────────────────────────


@pytest.fixture
def restore_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the restore trigger + state sidecar at a tmp dir + force the
    appliance deployment kind so the gate passes."""
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(
        appliance_state, "_CLUSTER_RESTORE_TRIGGER_FILE", tmp_path / "cluster-restore-pending"
    )
    monkeypatch.setattr(
        appliance_state, "_CLUSTER_RESTORE_STATE_SIDECAR", tmp_path / "cluster-restore.state"
    )
    return tmp_path


def test_restore_writes_trigger_with_marker(restore_paths: Path) -> None:
    assert appliance_state.maybe_fire_cluster_restore("snap-A") is True
    body = (restore_paths / "cluster-restore-pending").read_text()
    # Two lines: the guardrail confirm marker, then the snapshot name.
    assert body == f"{appliance_state._CLUSTER_RESTORE_CONFIRM}\nsnap-A\n"


def test_restore_idempotent_until_consumed(restore_paths: Path) -> None:
    assert appliance_state.maybe_fire_cluster_restore("snap-A") is True
    assert appliance_state.maybe_fire_cluster_restore("snap-A") is False


def test_restore_requires_snapshot(restore_paths: Path) -> None:
    assert appliance_state.maybe_fire_cluster_restore(None) is False
    assert appliance_state.maybe_fire_cluster_restore("") is False
    assert not (restore_paths / "cluster-restore-pending").exists()


def test_restore_skipped_off_appliance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    monkeypatch.setattr(
        appliance_state, "_CLUSTER_RESTORE_TRIGGER_FILE", tmp_path / "cluster-restore-pending"
    )
    assert appliance_state.maybe_fire_cluster_restore("snap-A") is False
    assert not (tmp_path / "cluster-restore-pending").exists()


def test_read_cluster_restore_state(restore_paths: Path) -> None:
    assert appliance_state.read_cluster_restore_state() == (None, None)
    (restore_paths / "cluster-restore.state").write_text("restoring\tsnap-A")
    assert appliance_state.read_cluster_restore_state() == ("restoring", "snap-A")
    (restore_paths / "cluster-restore.state").write_text("done\t")
    assert appliance_state.read_cluster_restore_state() == ("done", None)
