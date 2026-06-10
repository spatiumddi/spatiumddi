"""Supervisor resolver config-reload trigger writer + status reader (#158).

Covers the appliance-only gate, the payload shape (2 header lines + the
rendered drop-in body), hash-based idempotency, the disabled (automatic)
revert path, and the ``read_resolver_status`` sidecar parser. The actual
systemd-resolved reload is the host-side runner (spatiumddi-resolved-reload).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def resolver_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(
        appliance_state, "_RESOLVER_TRIGGER_FILE", tmp_path / "resolver-config-pending"
    )
    monkeypatch.setattr(
        appliance_state, "_RESOLVER_HASH_SIDECAR", tmp_path / "resolver-config-hash"
    )
    monkeypatch.setattr(
        appliance_state, "_RESOLVER_STATUS_SIDECAR", tmp_path / "resolver-status"
    )
    return tmp_path


_BODY = "[Resolve]\nDNS=1.1.1.1\nDomains=~.\nDNSSEC=allow-downgrade\nDNSOverTLS=no\n"
_BLOCK = {
    "enabled": True,
    "config_hash": "abc123",
    "resolved_conf": _BODY,
}


def test_enabled_writes_payload(resolver_paths: Path) -> None:
    assert appliance_state.maybe_fire_resolver_reload(_BLOCK) is True
    body = (resolver_paths / "resolver-config-pending").read_text()
    lines = body.split("\n")
    assert lines[0] == "enabled"
    assert lines[1] == "abc123"
    # The header is 2 lines; the rest is the rendered drop-in body.
    rest = body.split("\n", 2)[2]
    assert rest == _BODY


def test_idempotent_until_consumed(resolver_paths: Path) -> None:
    assert appliance_state.maybe_fire_resolver_reload(_BLOCK) is True
    assert appliance_state.maybe_fire_resolver_reload(_BLOCK) is False


def test_hash_unchanged_does_not_fire(resolver_paths: Path) -> None:
    (resolver_paths / "resolver-config-hash").write_text("abc123\n")
    assert appliance_state.maybe_fire_resolver_reload(_BLOCK) is False
    assert not (resolver_paths / "resolver-config-pending").exists()


def test_disabled_block_fires_when_previously_applied(resolver_paths: Path) -> None:
    # Previously applied an override; now revert to automatic — the
    # disabled-shape block must fire so the runner removes the drop-in.
    (resolver_paths / "resolver-config-hash").write_text("abc123\n")
    disabled = {"enabled": False, "config_hash": "", "resolved_conf": ""}
    assert appliance_state.maybe_fire_resolver_reload(disabled) is True
    body = (resolver_paths / "resolver-config-pending").read_text()
    assert body.split("\n")[0] == "disabled"


def test_non_appliance_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    monkeypatch.setattr(
        appliance_state, "_RESOLVER_TRIGGER_FILE", tmp_path / "resolver-config-pending"
    )
    assert appliance_state.maybe_fire_resolver_reload(_BLOCK) is False
    assert not (tmp_path / "resolver-config-pending").exists()


def test_read_resolver_status_parses_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecar = tmp_path / "resolver-status"
    monkeypatch.setattr(appliance_state, "_RESOLVER_STATUS_SIDECAR", sidecar)
    # Missing sidecar → None (backend leaves the column alone).
    assert appliance_state.read_resolver_status() is None
    sidecar.write_text("override\n")
    assert appliance_state.read_resolver_status() == "override"
    sidecar.write_text("automatic\n")
    assert appliance_state.read_resolver_status() == "automatic"
    sidecar.write_text("failed\n")
    assert appliance_state.read_resolver_status() == "failed"
    # Garbage → None.
    sidecar.write_text("nope\n")
    assert appliance_state.read_resolver_status() is None
