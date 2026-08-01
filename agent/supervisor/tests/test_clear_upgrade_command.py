"""#786 — the operator-driven "make the host forget a failed upgrade" command.

The visible failure state is re-published from these host sidecars on
every heartbeat, so clearing the DB alone never worked: the next
heartbeat restored it, and rebooting didn't help because release-state
lives on the persistent /var a slot write never touches.

The passive heal (``clear_fleet_upgrade_marker``) can't cover the wedged
case either — it returns early whenever a trigger file is present, which
is exactly the state a stranded apply leaves behind, and that same guard
also blocks ``maybe_fire_fleet_upgrade``. So a box could neither clear
nor re-apply.

``apply_clear_upgrade_command`` is the explicit counterpart: the operator
asked, so the stranded trigger goes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    state = tmp_path / "slot-upgrade-pending.state"
    trigger = tmp_path / "slot-upgrade-pending"
    progress = tmp_path / "slot-upgrade.progress"
    marker = tmp_path / "slot-upgrade-fired-url"
    monkeypatch.setattr(appliance_state, "_HOST_SLOT_STATE", state)
    monkeypatch.setattr(appliance_state, "_TRIGGER_FILE", trigger)
    monkeypatch.setattr(appliance_state, "_SLOT_UPGRADE_PROGRESS", progress)
    monkeypatch.setattr(appliance_state, "_FIRED_URL_MARKER", marker)
    # The command is appliance-only; every other deployment kind no-ops.
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "appliance")
    return {"state": state, "trigger": trigger, "progress": progress, "marker": marker}


def _wedged(paths: dict[str, Path]) -> None:
    """The shape a stranded failure leaves on disk."""
    paths["state"].write_text("failed 2026-08-01T00:00:00+00:00\n", encoding="utf-8")
    paths["progress"].write_text(json.dumps({"step": "failed", "pct": 40}), encoding="utf-8")
    paths["trigger"].write_text("https://cp.local/x.raw.xz\n", encoding="utf-8")
    paths["marker"].write_text("https://cp.local/x.raw.xz\n", encoding="utf-8")


def test_clear_resets_every_sidecar_and_removes_the_trigger(paths: dict[str, Path]) -> None:
    _wedged(paths)

    assert appliance_state.apply_clear_upgrade_command() is True

    assert not paths["trigger"].exists(), "a stranded trigger must be removed, not deferred to"
    assert not paths["marker"].exists()
    assert not paths["progress"].exists()
    assert paths["state"].read_text(encoding="utf-8").startswith("ready ")


def test_clear_removes_the_trigger_the_passive_heal_refuses_to_touch(
    paths: dict[str, Path],
) -> None:
    """The regression this exists for: with a trigger on disk the passive
    heal returns early and the box stays wedged forever."""
    _wedged(paths)

    appliance_state.clear_fleet_upgrade_marker()
    assert paths["state"].read_text(encoding="utf-8").startswith("failed"), (
        "passive heal is expected to bail while a trigger exists"
    )

    appliance_state.apply_clear_upgrade_command()
    assert paths["state"].read_text(encoding="utf-8").startswith("ready ")


def test_clear_prunes_every_failed_sidecar(paths: dict[str, Path]) -> None:
    """#386's runaway re-fire left piles of these; an explicit clear takes
    them all, not all-but-five."""
    for ts in range(9):
        (paths["trigger"].parent / f"slot-upgrade-pending.failed.{ts}").write_text("x")

    appliance_state.apply_clear_upgrade_command()

    assert list(paths["trigger"].parent.glob("slot-upgrade-pending.failed.*")) == []


def test_clear_is_idempotent_on_an_already_clean_host(paths: dict[str, Path]) -> None:
    """Nothing to reset → False, so the heartbeat doesn't log a reset that
    didn't happen. Must not raise on the missing files."""
    assert appliance_state.apply_clear_upgrade_command() is False


def test_clear_is_a_noop_off_appliance(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(appliance_state, "detect_deployment_kind", lambda: "docker")
    _wedged(paths)

    assert appliance_state.apply_clear_upgrade_command() is False
    assert paths["trigger"].exists()
    assert paths["state"].read_text(encoding="utf-8").startswith("failed")
