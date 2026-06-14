"""#421 — per-box slot reader ages a stale in-flight to failed.

A SIGKILL / power-loss apply on the control-plane's own host can't run the
runner's failed-on-exit trap, so ``_upgrade_state_now`` would report
``in-flight`` forever — which 409s a per-box re-apply / rollback via
``is_apply_in_flight()``. The reader now ages the runner's liveness stamp
(re-stamped every ~60s while alive) and reports ``failed`` once it goes
stale, mirroring the supervisor's ``_reap_stale_inflight`` threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.appliance import slot


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    state = tmp_path / "slot-upgrade-pending.state"
    trigger = tmp_path / "slot-upgrade-pending"
    monkeypatch.setattr(slot, "_STATE_FILE", state)
    monkeypatch.setattr(slot, "_TRIGGER_FILE", trigger)
    return {"state": state, "trigger": trigger}


def _iso_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


# ── _stamp_is_stale ──────────────────────────────────────────────────────


def test_stamp_fresh_not_stale() -> None:
    assert slot._stamp_is_stale(_iso_ago(5)) is False


def test_stamp_old_is_stale() -> None:
    assert slot._stamp_is_stale(_iso_ago(slot._STALE_INFLIGHT_SECONDS + 60)) is True


@pytest.mark.parametrize("bad", [None, "", "not-a-date"])
def test_stamp_unparseable_not_stale(bad: str | None) -> None:
    assert slot._stamp_is_stale(bad) is False


def test_stamp_naive_treated_as_utc() -> None:
    naive = (datetime.now(UTC) - timedelta(seconds=600)).replace(tzinfo=None)
    assert slot._stamp_is_stale(naive.isoformat()) is True


# ── _upgrade_state_now ───────────────────────────────────────────────────


def test_live_inflight_reported_inflight(paths: dict[str, Path]) -> None:
    paths["trigger"].write_text("https://example/img.raw.xz\n")
    paths["state"].write_text(f"in-flight {_iso_ago(10)}\n")
    state, _ = slot._upgrade_state_now()
    assert state == "in-flight"


def test_stale_inflight_reported_failed(paths: dict[str, Path]) -> None:
    paths["trigger"].write_text("https://example/img.raw.xz\n")
    paths["state"].write_text(
        f"in-flight {_iso_ago(slot._STALE_INFLIGHT_SECONDS + 120)}\n"
    )
    state, _ = slot._upgrade_state_now()
    assert state == "failed"
    # And so the per-box apply path is unblocked.
    assert slot.is_apply_in_flight() is False


def test_done_passes_through(paths: dict[str, Path]) -> None:
    paths["state"].write_text(f"done {_iso_ago(99999)}\n")
    state, _ = slot._upgrade_state_now()
    assert state == "done"


def test_failed_with_renamed_trigger_heals_to_ready(paths: dict[str, Path]) -> None:
    # Existing heal: failed + trigger already renamed away → green.
    paths["state"].write_text(f"failed {_iso_ago(10)}\n")
    state, _ = slot._upgrade_state_now()
    assert state == "ready"
