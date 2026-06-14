"""#421 — supervisor-side staleness backstop for slot upgrades.

A slot-upgrade apply that dies mid-flight (SIGKILL, OOM-killed dd, power
loss) can't run the host runner's failed-on-exit trap, so the .state
sidecar stays ``in-flight`` forever. The host runner re-stamps the
in-flight marker every ~60s while it's alive, so a stamp older than
``_STALE_INFLIGHT_SECONDS`` means the runner is gone. ``_reap_stale_
inflight`` surfaces that as ``failed`` and heals the sidecar/trigger so
the operator can clear + re-apply.

These tests pin the false-positive boundary (a live, slow apply keeps the
stamp fresh and must never be reaped) and the heal side effects.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spatium_supervisor import appliance_state


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    state = tmp_path / "slot-upgrade-pending.state"
    trigger = tmp_path / "slot-upgrade-pending"
    progress = tmp_path / "slot-upgrade.progress"
    monkeypatch.setattr(appliance_state, "_HOST_SLOT_STATE", state)
    monkeypatch.setattr(appliance_state, "_TRIGGER_FILE", trigger)
    monkeypatch.setattr(appliance_state, "_SLOT_UPGRADE_PROGRESS", progress)
    return {"state": state, "trigger": trigger, "progress": progress}


def _ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def test_fresh_inflight_is_not_reaped(paths: dict[str, Path]) -> None:
    """A live apply re-stamps every ~60s — a fresh stamp must stay
    in-flight (no false failure on a slow-but-running upgrade)."""
    stamp = _ago(5)
    state, when = appliance_state._reap_stale_inflight("in-flight", stamp)
    assert state == "in-flight"
    assert when == stamp
    # No files written — the reaper was a no-op.
    assert not paths["state"].exists()
    assert not paths["progress"].exists()


def test_just_under_threshold_not_reaped(paths: dict[str, Path]) -> None:
    state, _ = appliance_state._reap_stale_inflight(
        "in-flight", _ago(appliance_state._STALE_INFLIGHT_SECONDS - 30)
    )
    assert state == "in-flight"


def test_stale_inflight_is_reaped_to_failed(paths: dict[str, Path]) -> None:
    """A stamp older than the threshold means the runner died — report
    failed and heal the sidecar + trigger so clear/re-apply works."""
    paths["state"].write_text("in-flight 2020-01-01T00:00:00+00:00\n")
    paths["trigger"].write_text("https://example/img.raw.xz\n")
    stamp = _ago(appliance_state._STALE_INFLIGHT_SECONDS + 60)

    state, when = appliance_state._reap_stale_inflight("in-flight", stamp)

    assert state == "failed"
    assert when is not None and when.tzinfo is not None
    # Sidecar rewritten to a terminal failed state with a fresh stamp.
    assert paths["state"].read_text().startswith("failed ")
    # Progress breadcrumb explains why.
    prog = json.loads(paths["progress"].read_text())
    assert prog["step"] == "failed"
    assert "without completing" in prog["detail"]
    # Lingering trigger renamed out of the way so a re-apply / Cancel
    # isn't blocked by clear_fleet_upgrade_marker's "trigger present" guard.
    assert not paths["trigger"].exists()
    renamed = list(paths["trigger"].parent.glob("slot-upgrade-pending.failed.*"))
    assert len(renamed) == 1


def test_stale_inflight_without_trigger_present(paths: dict[str, Path]) -> None:
    """Power-loss case: on reboot the .state persists but the trigger may
    already be gone. Reap still reports failed and rewrites the sidecar."""
    paths["state"].write_text("in-flight 2020-01-01T00:00:00+00:00\n")
    state, _ = appliance_state._reap_stale_inflight(
        "in-flight", _ago(appliance_state._STALE_INFLIGHT_SECONDS + 60)
    )
    assert state == "failed"
    assert paths["state"].read_text().startswith("failed ")


def test_naive_stamp_treated_as_utc(paths: dict[str, Path]) -> None:
    """Old-format stamps without a tz offset must still age correctly
    (treated as UTC) rather than crashing on aware/naive subtraction."""
    naive = (datetime.now(UTC) - timedelta(seconds=600)).replace(tzinfo=None)
    state, _ = appliance_state._reap_stale_inflight("in-flight", naive)
    assert state == "failed"


@pytest.mark.parametrize("terminal", ["done", "failed", "ready"])
def test_non_inflight_states_untouched(paths: dict[str, Path], terminal: str) -> None:
    """Only in-flight is reaped — done/failed/ready pass through verbatim
    regardless of how old their stamp is."""
    stamp = _ago(99_999)
    state, when = appliance_state._reap_stale_inflight(terminal, stamp)
    assert state == terminal
    assert when == stamp
    assert not paths["state"].exists()


def test_inflight_without_stamp_is_conservative(paths: dict[str, Path]) -> None:
    """A stamp-less in-flight (old-format sidecar) can't be aged — leave
    it alone rather than risk failing a live apply."""
    state, when = appliance_state._reap_stale_inflight("in-flight", None)
    assert state == "in-flight"
    assert when is None
    assert not paths["state"].exists()


def test_reports_failed_even_if_persist_fails(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """If the durable sidecar write fails, still report failed this tick
    (the next heartbeat re-derives + retries)."""

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only fs")

    monkeypatch.setattr(type(paths["state"]), "write_text", boom)
    state, when = appliance_state._reap_stale_inflight(
        "in-flight", _ago(appliance_state._STALE_INFLIGHT_SECONDS + 60)
    )
    assert state == "failed"
    assert when is not None
