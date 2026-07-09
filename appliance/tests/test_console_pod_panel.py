"""The console Pods panel must show the whole cluster (#592).

On a 3-node HA appliance each node's ``spatium-console`` showed only that
node's pods, while ``kubectl get pods -A`` showed all of them. Operators who
use the console as their monitoring surface reasonably concluded it was
broken.

The cause was an auto-filter that kicked in above 20 visible pods and kept
"problem pods + pods on THIS node". A 3-node appliance idles at ~22 visible
pods, so the filter was effectively always on for exactly the deployment
where cluster-wide visibility matters most.

It also protected nothing. ``render_services`` sorts by
``_pod_sort_priority`` BEFORE the height clamp, so a crash-looping pod floats
to the top and cannot be clipped, and the remainder becomes the
"… +N more pods · F3 to list" subtitle. These tests pin both halves: the
panel shows every node, AND problems still survive a brutal clamp.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_console_pod_panel.py -v

Needs ``rich`` + ``psutil`` (the console's own deps). No cluster, no
appliance — the panel is rendered to a string and inspected.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).parent.parent
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatium-console"
)


@pytest.fixture(scope="module")
def console() -> ModuleType:
    """Import the console (it has no .py suffix and no import side effects)."""
    loader = importlib.machinery.SourceFileLoader("spatium_console", str(SCRIPT))
    spec = importlib.util.spec_from_loader("spatium_console", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _pod(name: str, node: str, state: str = "Running", ready: str = "1/1") -> dict:
    return {
        "Namespace": "spatium",
        "Names": name,
        "Type": "Deployment",
        "Node": node,
        "Ready": ready,
        "Restarts": 0,
        "RestartedAt": "",
        "K8sState": state,
        "PodIP": "10.42.0.1",
        "Ports": "",
        "Age": "5m",
    }


def _idle_three_node_cluster() -> list[dict]:
    """~22 visible pods spread over three nodes — what a real 3-node
    appliance idles at, and what tripped the old 20-pod threshold."""
    rows: list[dict] = []
    for node in ("ddi1", "ddi2", "ddi3"):
        for i in range(7):
            rows.append(_pod(f"workload-{node}-{i}", node))
    rows.append(_pod("migrate-abc", "ddi3", state="Succeeded", ready="0/1"))
    return rows


def _render(console: ModuleType, rows: list[dict], cap: int | None = None) -> str:
    """Render the Pods panel alone (bypasses render_frame's layout sizing)."""
    import io

    from rich.console import Console

    panel = console.render_services("control-plane", rows, cap=cap)
    buf = Console(width=200, record=True, file=io.StringIO())
    buf.print(panel)
    return buf.export_text()


class _StubTail:
    """JournalTail stand-in — render_log only reads ``.lines``."""

    lines: list[str] = []

    def stop(self) -> None:  # pragma: no cover - never called in tests
        pass


def _render_frame(console: ModuleType, rows: list[dict]) -> str:
    """Render the WHOLE frame. This is the path that carried the bug: the
    filter lived in render_frame's caller-side helper, not in
    render_services, so a panel-only test cannot see the regression."""
    import io

    from rich.console import Console

    state = console.DashboardState(env={}, tail=_StubTail(), tty_path="/dev/tty1")
    state.ps_rows = rows
    buf = Console(width=220, height=70, record=True, file=io.StringIO())
    buf.print(console.render_frame(state, 0.0))
    return buf.export_text()


def test_frame_shows_pods_from_nodes_other_than_this_one(console: ModuleType) -> None:
    """REGRESSION (#592). The exact symptom, exercised through render_frame.

    The old filter kept "problem pods + pods whose Node == this host". None of
    the fixture's nodes are named after the machine running the test, so the
    pre-fix code renders ZERO workload pods here — the same defect that, on a
    real ddi1, rendered only ddi1's third of the cluster.

    Asserting "at least two distinct nodes" rather than "all three": 21 pods
    exceed the panel height, so the last node legitimately overflows into the
    "+N more pods" subtitle. That is the clamp doing its job, not a filter.
    """
    text = _render_frame(console, _idle_three_node_cluster())
    nodes_seen = [n for n in ("ddi1", "ddi2", "ddi3") if f"workload-{n}-0" in text]
    assert len(nodes_seen) >= 2, f"only saw pods from {nodes_seen or 'no nodes'}"
    assert "filtered:" not in text, "the node-local filter label is back"


def test_frame_still_reports_overflow(console: ModuleType) -> None:
    """The subtitle is the operator's cue that the panel is truncated — it is
    what makes dropping the filter safe."""
    text = _render_frame(console, _idle_three_node_cluster())
    assert "more pods" in text
    assert "F3 to list" in text


def test_no_node_local_filter_survives(console: ModuleType) -> None:
    """The filter is gone, not merely re-tuned. A higher threshold would
    reintroduce the bug on a 5- or 7-node cluster."""
    assert not hasattr(console, "_filtered_pods")
    assert not hasattr(console, "_POD_FILTER_THRESHOLD")


def test_completed_jobs_still_hidden(console: ModuleType) -> None:
    """Dropping the node filter must not drop the Succeeded-Job filter —
    that one is load-bearing for panel height (see visible_pods)."""
    text = _render(console, _idle_three_node_cluster())
    assert "migrate-abc" not in text


def test_problem_pods_sort_above_healthy_ones(console: ModuleType) -> None:
    """The mechanism that actually protects a crash-looping pod: priority
    sort. 0 = broken, 1 = pending, 2 = not-ready, 3 = healthy."""
    crash = _pod("boom", "ddi3", state="CrashLoopBackOff", ready="0/1")
    pending = _pod("waiting", "ddi2", state="Pending", ready="0/1")
    notready = _pod("half", "ddi2", state="Running", ready="1/2")
    healthy = _pod("fine", "ddi1")
    assert console._pod_sort_priority(crash) == 0
    assert console._pod_sort_priority(pending) == 1
    assert console._pod_sort_priority(notready) == 2
    assert console._pod_sort_priority(healthy) == 3


def test_a_crashloop_on_a_remote_node_survives_a_brutal_clamp(
    console: ModuleType,
) -> None:
    """The filter's stated purpose was stopping a crash-looping pod being
    buried under healthy ones. Sort + clamp already guarantee that — even
    when the broken pod is on a REMOTE node and the panel can show 1 row.

    Without the sort this would render some arbitrary healthy pod instead,
    which is the failure the filter was invented to prevent.
    """
    rows = _idle_three_node_cluster()
    rows.append(_pod("boom", "ddi3", state="CrashLoopBackOff", ready="0/1"))
    text = _render(console, rows, cap=1)
    assert "boom" in text, "the crash-looping pod was clipped by the height clamp"
    assert "workload-ddi1-0" not in text, "a healthy pod outranked the crashloop"


def test_clamped_rows_are_reported_not_silently_dropped(console: ModuleType) -> None:
    """The overflow count is what tells the operator the panel is truncated.
    render_services derives it from what it actually dropped."""
    rows = _idle_three_node_cluster()
    text = _render(console, rows, cap=3)
    # 21 visible (the Succeeded job is filtered out), 3 shown → 18 hidden.
    assert "+18 more pods" in text
    assert "F3 to list" in text
