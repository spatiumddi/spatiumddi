"""Supervisor console-mode trigger writer (#393).

``maybe_fire_console_mode`` maps platform_settings.console_mode →
the grubenv ``spatium_verbose`` numeric the host runner + grub.cfg
consume (dashboard→0 / text_console→1 / verbose_dashboard→2), and
writes the verbose-boot trigger only on a real change (idempotent via
the applied sidecar). Replaces the pre-#393 boolean verbose_boot path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def cm_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    monkeypatch.setattr(
        appliance_state, "_VERBOSE_TRIGGER_FILE", tmp_path / "verbose-boot-pending"
    )
    monkeypatch.setattr(
        appliance_state, "_VERBOSE_APPLIED_FILE", tmp_path / "verbose-boot-applied"
    )
    return tmp_path


def _trigger(p: Path) -> str:
    return (p / "verbose-boot-pending").read_text().strip()


def test_text_console_maps_to_1(cm_paths: Path) -> None:
    assert appliance_state.maybe_fire_console_mode("text_console") is True
    assert _trigger(cm_paths) == "1"


def test_verbose_dashboard_maps_to_2(cm_paths: Path) -> None:
    assert appliance_state.maybe_fire_console_mode("verbose_dashboard") is True
    assert _trigger(cm_paths) == "2"


def test_dashboard_is_default_no_fire_from_fresh(cm_paths: Path) -> None:
    # Fresh box: applied sidecar missing → treated as "0"; dashboard → "0",
    # so there's nothing to change.
    assert appliance_state.maybe_fire_console_mode("dashboard") is False
    assert not (cm_paths / "verbose-boot-pending").exists()


def test_unknown_mode_falls_back_to_dashboard(cm_paths: Path) -> None:
    # Fail-closed: an unknown / None mode maps to "0" (dashboard).
    assert appliance_state.maybe_fire_console_mode("bogus") is False
    assert appliance_state.maybe_fire_console_mode(None) is False


def test_idempotent_against_applied_sidecar(cm_paths: Path) -> None:
    (cm_paths / "verbose-boot-applied").write_text("1\n")
    # Already applied text_console (1) → no re-fire.
    assert appliance_state.maybe_fire_console_mode("text_console") is False
    assert not (cm_paths / "verbose-boot-pending").exists()
    # But switching to verbose_dashboard (2) fires.
    assert appliance_state.maybe_fire_console_mode("verbose_dashboard") is True
    assert _trigger(cm_paths) == "2"


def test_non_appliance_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    monkeypatch.setattr(
        appliance_state, "_VERBOSE_TRIGGER_FILE", tmp_path / "verbose-boot-pending"
    )
    assert appliance_state.maybe_fire_console_mode("text_console") is False
    assert not (tmp_path / "verbose-boot-pending").exists()
