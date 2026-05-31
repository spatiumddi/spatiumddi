"""Supervisor LLDP config-reload trigger writer (#343).

Covers the appliance-only gate, the 4-section payload shape (LLDP carries
an extra daemon_args line vs SNMP/NTP), hash-based idempotency, and the
status-sidecar reader. The actual lldpd reload is the host-side runner
(spatiumddi-lldp-reload).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def lldp_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(appliance_state, "_LLDP_TRIGGER_FILE", tmp_path / "lldp-config-pending")
    monkeypatch.setattr(appliance_state, "_LLDP_HASH_SIDECAR", tmp_path / "lldp-config-hash")
    monkeypatch.setattr(appliance_state, "_LLDP_STATUS_SIDECAR", tmp_path / "lldp-status")
    return tmp_path


_BLOCK = {
    "enabled": True,
    "config_hash": "abc123",
    "lldpd_conf": "configure lldp tx-interval 30\n",
    "daemon_args": "-c -e",
}


def test_enabled_writes_four_section_payload(lldp_paths: Path) -> None:
    assert appliance_state.maybe_fire_lldp_reload(_BLOCK) is True
    body = (lldp_paths / "lldp-config-pending").read_text()
    lines = body.split("\n")
    assert lines[0] == "enabled"
    assert lines[1] == "abc123"
    assert lines[2] == "-c -e"  # daemon_args — the LLDP-specific 3rd line
    assert "configure lldp tx-interval 30" in body


def test_idempotent_until_consumed(lldp_paths: Path) -> None:
    assert appliance_state.maybe_fire_lldp_reload(_BLOCK) is True
    # Trigger already present → don't stack.
    assert appliance_state.maybe_fire_lldp_reload(_BLOCK) is False


def test_hash_unchanged_does_not_fire(lldp_paths: Path) -> None:
    (lldp_paths / "lldp-config-hash").write_text("abc123\n")
    # Sidecar hash matches the bundle hash → nothing to apply.
    assert appliance_state.maybe_fire_lldp_reload(_BLOCK) is False
    assert not (lldp_paths / "lldp-config-pending").exists()


def test_disabled_block_fires_when_previously_applied(lldp_paths: Path) -> None:
    (lldp_paths / "lldp-config-hash").write_text("abc123\n")
    disabled = {
        "enabled": False,
        "config_hash": "",
        "lldpd_conf": "",
        "daemon_args": "",
    }
    assert appliance_state.maybe_fire_lldp_reload(disabled) is True
    assert (lldp_paths / "lldp-config-pending").read_text().split("\n")[0] == "disabled"


def test_skipped_off_appliance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    monkeypatch.setattr(appliance_state, "_LLDP_TRIGGER_FILE", tmp_path / "lldp-config-pending")
    monkeypatch.setattr(appliance_state, "_LLDP_HASH_SIDECAR", tmp_path / "lldp-config-hash")
    assert appliance_state.maybe_fire_lldp_reload(_BLOCK) is False
    assert not (tmp_path / "lldp-config-pending").exists()


def test_read_lldpd_running(lldp_paths: Path) -> None:
    assert appliance_state.read_lldpd_running() is None  # sidecar missing
    (lldp_paths / "lldp-status").write_text("running\n")
    assert appliance_state.read_lldpd_running() is True
    (lldp_paths / "lldp-status").write_text("stopped\n")
    assert appliance_state.read_lldpd_running() is False
