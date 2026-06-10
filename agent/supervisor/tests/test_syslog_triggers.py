"""Supervisor syslog config-reload trigger writer + status reader (#156).

Covers the appliance-only gate, the payload shape (3 header lines + a JSON
CA blob + the conf body), hash-based idempotency, and the
``read_syslog_forwarding`` status-sidecar parser. The actual rsyslog
restart is the host-side runner (spatiumddi-syslog-reload).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def syslog_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(
        appliance_state, "_SYSLOG_TRIGGER_FILE", tmp_path / "syslog-config-pending"
    )
    monkeypatch.setattr(
        appliance_state, "_SYSLOG_HASH_SIDECAR", tmp_path / "syslog-config-hash"
    )
    monkeypatch.setattr(
        appliance_state, "_SYSLOG_STATUS_SIDECAR", tmp_path / "syslog-status"
    )
    return tmp_path


_BLOCK = {
    "enabled": True,
    "config_hash": "abc123",
    "rsyslog_conf": '*.* action(type="omfwd" target="siem")\n',
    "ca_certs": {"/etc/rsyslog.d/spatium-ca/target-0.pem": "PEMDATA\n"},
}


def test_enabled_writes_payload_with_ca_blob(syslog_paths: Path) -> None:
    assert appliance_state.maybe_fire_syslog_reload(_BLOCK) is True
    body = (syslog_paths / "syslog-config-pending").read_text()
    lines = body.split("\n")
    assert lines[0] == "enabled"
    assert lines[1] == "abc123"
    # Line 3 is the JSON CA blob.
    ca = json.loads(lines[2])
    assert ca["/etc/rsyslog.d/spatium-ca/target-0.pem"] == "PEMDATA\n"
    assert 'target="siem"' in body


def test_idempotent_until_consumed(syslog_paths: Path) -> None:
    assert appliance_state.maybe_fire_syslog_reload(_BLOCK) is True
    # Trigger already present → don't stack.
    assert appliance_state.maybe_fire_syslog_reload(_BLOCK) is False


def test_hash_unchanged_does_not_fire(syslog_paths: Path) -> None:
    (syslog_paths / "syslog-config-hash").write_text("abc123\n")
    # Sidecar hash matches the bundle hash → nothing to apply.
    assert appliance_state.maybe_fire_syslog_reload(_BLOCK) is False
    assert not (syslog_paths / "syslog-config-pending").exists()


def test_disabled_block_fires_when_previously_applied(syslog_paths: Path) -> None:
    (syslog_paths / "syslog-config-hash").write_text("abc123\n")
    disabled = {
        "enabled": False,
        "config_hash": "",
        "rsyslog_conf": "",
        "ca_certs": {},
    }
    # Empty hash != "abc123" → fire a disable trigger.
    assert appliance_state.maybe_fire_syslog_reload(disabled) is True
    body = (syslog_paths / "syslog-config-pending").read_text()
    assert body.split("\n")[0] == "disabled"


def test_non_appliance_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    monkeypatch.setattr(
        appliance_state, "_SYSLOG_TRIGGER_FILE", tmp_path / "syslog-config-pending"
    )
    assert appliance_state.maybe_fire_syslog_reload(_BLOCK) is False
    assert not (tmp_path / "syslog-config-pending").exists()


def test_read_syslog_forwarding_parses_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = tmp_path / "syslog-status"
    monkeypatch.setattr(appliance_state, "_SYSLOG_STATUS_SIDECAR", sidecar)
    # Missing sidecar → None (backend leaves the column alone).
    assert appliance_state.read_syslog_forwarding() is None
    for value in ("forwarding", "unreachable", "disabled"):
        sidecar.write_text(value + "\n")
        assert appliance_state.read_syslog_forwarding() == value
    # Garbage → None.
    sidecar.write_text("nonsense\n")
    assert appliance_state.read_syslog_forwarding() is None
