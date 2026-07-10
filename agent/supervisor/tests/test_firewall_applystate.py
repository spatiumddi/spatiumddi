"""Supervisor firewall apply-state read-back (#285 Phase 2b).

The host runner writes firewall-applied-hash / -status / -base-marker
sidecars; the supervisor echoes them on the heartbeat. These cover the
sidecar readers + that collect() only emits them on an appliance.
"""

from __future__ import annotations

import pytest

from spatium_supervisor import appliance_state

_ATTRS = (
    "_FIREWALL_APPLIED_HASH_SIDECAR",
    "_FIREWALL_APPLIED_STATUS_SIDECAR",
    "_FIREWALL_BASE_MARKER_SIDECAR",
)


def test_readers_parse_present(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    h = tmp_path / "hash"
    h.write_text("a" * 64 + "\n")
    s = tmp_path / "status"
    s.write_text("ok\n")
    m = tmp_path / "marker"
    m.write_text("b" * 64 + "\n")
    monkeypatch.setattr(appliance_state, "_FIREWALL_APPLIED_HASH_SIDECAR", h)
    monkeypatch.setattr(appliance_state, "_FIREWALL_APPLIED_STATUS_SIDECAR", s)
    monkeypatch.setattr(appliance_state, "_FIREWALL_BASE_MARKER_SIDECAR", m)
    assert appliance_state.read_firewall_applied_hash() == "a" * 64
    assert appliance_state.read_firewall_applied_status() == "ok"
    assert appliance_state.read_firewall_base_marker() == "b" * 64


def test_readers_none_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for attr in _ATTRS:
        monkeypatch.setattr(appliance_state, attr, tmp_path / "absent")
    assert appliance_state.read_firewall_applied_hash() is None
    assert appliance_state.read_firewall_applied_status() is None
    assert appliance_state.read_firewall_base_marker() is None


def test_readers_none_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    e = tmp_path / "empty"
    e.write_text("   \n")
    for attr in _ATTRS:
        monkeypatch.setattr(appliance_state, attr, e)
    assert appliance_state.read_firewall_applied_status() is None


def test_collect_off_appliance_emits_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # On a non-appliance deploy collect() must send None for all three so
    # the backend's "only-when-not-None" upsert never blanks the columns.
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    monkeypatch.setattr(appliance_state, "detect_runtime", lambda: "docker")
    out = appliance_state.collect()
    assert out["firewall_applied_hash"] is None
    assert out["firewall_applied_status"] is None
    assert out["firewall_base_marker"] is None


def test_refusal_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    sidecar = tmp_path / "sub" / "firewall-refused"  # parent absent → mkdir path
    monkeypatch.setattr(appliance_state, "_FIREWALL_REFUSAL_SIDECAR", sidecar)
    assert appliance_state.read_firewall_state() is None
    appliance_state.record_firewall_refusal("in-pod", "no etcd peer rule")
    state = appliance_state.read_firewall_state()
    assert state == {
        "state": "refused_self_partition",
        "source": "in-pod",
        "reason": "no etcd peer rule",
    }
    appliance_state.clear_firewall_refusal()
    assert appliance_state.read_firewall_state() is None


def test_refusal_persist_failure_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """#611 — a genuine write failure is the one way a self-partition refusal
    can go silent on the host-side reader, so it must warn (not debug)."""
    # A file where the sidecar's grandparent dir should be makes mkdir raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setattr(
        appliance_state, "_FIREWALL_REFUSAL_SIDECAR", blocker / "deep" / "refused"
    )
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        appliance_state.log, "warning", lambda event, **kw: calls.append((event, kw))
    )
    appliance_state.record_firewall_refusal("in-pod", "boom")  # must not raise
    assert calls and calls[0][0] == "supervisor.firewall.refusal_persist_failed"
    assert "error" in calls[0][1] and "path" in calls[0][1]
