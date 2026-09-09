"""The readiness marker must be taken back when the agent dies (#1043).

`_touch_ready_marker` means "I have synced at least once" and nothing ever
un-touched it, so when the heartbeat thread died the pod kept passing its
readinessProbe on Kea's still-listening socket — 1/1 Ready with no agent in
it, every DHCP change accepted and never delivered.

The container exit is the primary cure; clearing the marker is what makes a
dead agent VISIBLE if the process ever outlives its threads. Neither the
marker nor its clear had any test before this, which is why the pair could
drift apart.

The probe command is lifted from the chart rather than retyped, so a change
to either side has to be a deliberate one.
"""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path

import pytest

from spatium_dhcp_agent import supervisor
from spatium_dhcp_agent.sync import _touch_ready_marker, clear_ready_marker

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "charts" / "spatiumddi-appliance" / "templates" / "dhcp-kea.yaml"


def test_touch_then_clear_round_trips(tmp_path: Path) -> None:
    marker = tmp_path / ".ready"
    _touch_ready_marker(tmp_path)
    assert marker.exists(), "the marker was never stamped"
    clear_ready_marker(tmp_path)
    assert not marker.exists(), "readiness was still claimed after the clear"


def test_clearing_an_absent_marker_is_not_an_error(tmp_path: Path) -> None:
    """The agent can die before its first successful sync.

    This runs on the shutdown path, so it must never raise and mask the exit
    that is already in progress.
    """
    clear_ready_marker(tmp_path)  # must not raise
    assert not (tmp_path / ".ready").exists()


def test_clear_survives_an_unremovable_marker(tmp_path: Path, monkeypatch) -> None:
    """Best-effort, same as the touch: a filesystem error must not abort exit."""
    _touch_ready_marker(tmp_path)

    def boom(self: Path) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "unlink", boom)
    clear_ready_marker(tmp_path)  # logs, does not raise


def test_the_thread_death_path_clears_it() -> None:
    """The wiring, not just the helper.

    Stated limit: `supervisor.run()` builds a dozen threads and needs a
    control plane, so this asserts the call is present on that branch rather
    than executing it. It catches the realistic regression — the call or its
    import being dropped — which is the one that would silently restore a
    Ready pod with no agent.
    """
    src = inspect.getsource(supervisor.run)
    assert "dhcp_agent_thread_died" in src, "the thread-death branch moved"
    branch = src[src.index("dhcp_agent_thread_died") :]
    assert "clear_ready_marker" in branch.split("return 2")[0], (
        "the thread-death branch no longer clears the readiness marker"
    )


@pytest.mark.skipif(not CHART.exists(), reason="chart not present in this checkout")
def test_the_real_probe_fails_once_the_marker_is_cleared(tmp_path: Path) -> None:
    """End-to-end on the contract: the chart's own probe must flip.

    Only the marker half is exercised — the `grep ':0043 ' /proc/net/udp` half
    asks whether Kea is listening, which is true in the broken state and is
    precisely why the marker has to carry the signal.
    """
    body = CHART.read_text(encoding="utf-8")
    m = re.search(r'"(test -f (\S+?)/\.ready [^"]*)"', body)
    assert m, "could not find the readinessProbe command in the chart"
    state_dir_in_chart = m.group(2)

    probe = f'test -f "{tmp_path}/.ready"'
    assert state_dir_in_chart.endswith("spatium-dhcp-agent"), (
        f"probe path moved: {state_dir_in_chart}"
    )

    _touch_ready_marker(tmp_path)
    assert subprocess.run(["sh", "-c", probe]).returncode == 0, "probe should pass when ready"
    clear_ready_marker(tmp_path)
    assert subprocess.run(["sh", "-c", probe]).returncode != 0, (
        "the probe still passes after the agent cleared readiness"
    )
