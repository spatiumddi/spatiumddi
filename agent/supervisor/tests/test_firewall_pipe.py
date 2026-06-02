"""Supervisor firewall pipe — maybe_fire_firewall_reload (#285 Phase 2a).

When the control plane renders server-side, the supervisor pipes the body
to the firewall-pending trigger (2-line Phase-1 format) instead of
rendering in-pod. Empty config_hash = no authority → no-op (fall back).
"""

from __future__ import annotations

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def _appliance(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    trigger = tmp_path / "firewall-pending"
    applied = tmp_path / "firewall-applied-hash"
    monkeypatch.setattr(appliance_state, "_FIREWALL_TRIGGER_FILE", trigger)
    monkeypatch.setattr(appliance_state, "_FIREWALL_APPLIED_HASH_SIDECAR", applied)
    return trigger, applied


def test_fires_2line_trigger(_appliance) -> None:
    trigger, _ = _appliance
    body = "# spatium-bootstrap: keep\ntcp dport 22 accept\n"
    fired = appliance_state.maybe_fire_firewall_reload(
        {"enabled": True, "config_hash": "h1", "firewall_conf": body}
    )
    assert fired is True
    written = trigger.read_text()
    assert written == f"h1\n{body}"  # exact 2-line Phase-1 format


def test_empty_hash_is_noop(_appliance) -> None:
    trigger, _ = _appliance
    assert (
        appliance_state.maybe_fire_firewall_reload(
            {"enabled": False, "config_hash": "", "firewall_conf": ""}
        )
        is False
    )
    assert not trigger.exists()


def test_short_circuits_on_matching_applied_hash(_appliance) -> None:
    trigger, applied = _appliance
    applied.write_text("h1\n")
    fired = appliance_state.maybe_fire_firewall_reload(
        {"enabled": True, "config_hash": "h1", "firewall_conf": "body\n"}
    )
    assert fired is False  # already applied → no re-fire
    assert not trigger.exists()


def test_skips_when_trigger_present(_appliance) -> None:
    trigger, _ = _appliance
    trigger.write_text("stale\n")
    fired = appliance_state.maybe_fire_firewall_reload(
        {"enabled": True, "config_hash": "h2", "firewall_conf": "body\n"}
    )
    assert fired is False  # host runner hasn't consumed the prior trigger yet


def test_off_appliance_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    assert (
        appliance_state.maybe_fire_firewall_reload(
            {"enabled": True, "config_hash": "h", "firewall_conf": "b\n"}
        )
        is False
    )
