"""Unit tests for the appliance console dashboard (``spatium-console``).

The console is a standalone host script (Python + rich + psutil) living at
``appliance/mkosi.extra/usr/local/bin/spatium-console`` — it is NOT part of
the ``app`` package, so it is loaded by path here. The whole module skips
cleanly when rich / psutil aren't installed (e.g. the slim API test image) or
when the script isn't present in the checkout, so it never breaks CI; it runs
on the host / appliance where those deps exist.

Covers (Phase 2 INC 10 / P2.3): the pure formatting + classification helpers
(``_format_pod_age`` boundaries, ``_rst_verdict`` thresholds), the 5 s-tier
metric parsers against captured fixture strings (etcd / CNPG / API), and the
§3d sizing invariant — ``render_frame`` renders at every tier without an
exception and always keeps the vitals row + footer on-screen.
"""

from __future__ import annotations

import importlib.util
import io
import re
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

pytest.importorskip("rich")
pytest.importorskip("psutil")

from rich.console import Console  # noqa: E402  (after importorskip by design)

_CONSOLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "appliance"
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatium-console"
)

pytestmark = pytest.mark.skipif(
    not _CONSOLE_PATH.exists(),
    reason="spatium-console host script not present in this checkout",
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="module")
def m():
    """Load the console host script as an importable module (by path)."""
    loader = SourceFileLoader("spatium_console_under_test", str(_CONSOLE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _FakeResp:
    """Minimal urlopen() stand-in: ``.read()`` returns the fixture bytes."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode()


class _FakeTail:
    lines: list[str] = []


# ── pure helpers ──────────────────────────────────────────────────────────


def test_format_pod_age_units(m):
    assert m._format_pod_age("") == ""
    assert m._format_pod_age("not-a-timestamp") == ""
    now = datetime.now(UTC)

    def iso(delta: timedelta) -> str:
        return (now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert m._format_pod_age(iso(timedelta(seconds=30))).endswith("s")
    assert m._format_pod_age(iso(timedelta(minutes=5))) == "5m"
    assert m._format_pod_age(iso(timedelta(hours=3))) == "3h"
    assert m._format_pod_age(iso(timedelta(days=5))) == "5d"
    # A future timestamp clamps to 0s rather than going negative.
    assert m._format_pod_age((now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")) == "0s"


def test_rst_verdict_thresholds(m):
    recent = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(UTC) - timedelta(seconds=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert m._rst_verdict(0, "") == "quiet"
    # A currently-broken pod is red regardless of restart count / timestamp.
    assert m._rst_verdict(0, "", "CrashLoopBackOff") == "red"
    # Lifetime backstop (>= 10) is red even when quiet.
    assert m._rst_verdict(12, old) == "red"
    # Flapping: >= 3 restarts AND a recent one.
    assert m._rst_verdict(4, recent) == "red"
    # Restarted but quiet (recent restart but below the flap floor) → amber.
    assert m._rst_verdict(2, recent) == "amber"
    # Several restarts but not recent → amber, not red.
    assert m._rst_verdict(5, old) == "amber"


# ── 5 s-tier metric parsers (fixtures captured from the live cluster) ──────


def test_fetch_etcd_health_parses(m, monkeypatch):
    fixture = (
        "etcd_server_has_leader 1\n"
        "etcd_server_is_leader 1\n"
        "etcd_server_proposals_failed_total 0\n"
        "etcd_mvcc_db_total_size_in_use_in_bytes 4.939776e+06\n"
        "etcd_mvcc_db_total_size_in_bytes 8433664\n"
    )
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _FakeResp(fixture))
    out = m.fetch_etcd_health()
    assert out["has_leader"] == 1
    assert out["proposals_failed"] == 0
    assert out["db_in_use"] == pytest.approx(4_939_776.0)

    def _boom(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(m.urllib.request, "urlopen", _boom)
    assert m.fetch_etcd_health() == {}


def test_fetch_cnpg_metrics_parses(m, monkeypatch):
    fixture = (
        'cnpg_backends_total{datname="spatiumddi",state="idle"} 9\n'
        'cnpg_backends_total{datname="spatiumddi",state="active"} 1\n'
        "cnpg_backends_waiting_total 0\n"
        'cnpg_pg_database_size_bytes{datname="spatiumddi"} 18013207\n'
        "cnpg_pg_replication_streaming_replicas 0\n"
        "cnpg_pg_replication_in_recovery 0\n"
    )
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _FakeResp(fixture))
    out = m.fetch_cnpg_metrics("10.0.0.1")
    assert out["backends_idle"] == 9
    assert out["backends_active"] == 1
    assert out["backends_waiting"] == 0
    assert out["db_size_bytes"] == pytest.approx(18_013_207.0)
    assert out["in_recovery"] == 0


def test_fetch_api_metrics_parses(m, monkeypatch):
    fixture = (
        'spatiumddi_api_requests_total{path_template="/x",status_code="200"} 100\n'
        'spatiumddi_api_requests_total{path_template="/y",status_code="200"} 54\n'
        "spatiumddi_api_active_requests 2\n"
    )
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _FakeResp(fixture))
    # Reset the module-level prev-sample so rate is deterministic (0.0 first call).
    m._API_REQ_PREV = None
    out = m.fetch_api_metrics("10.0.0.1")
    assert out["total"] == 154
    assert out["active"] == 2
    assert out["rate"] == 0.0


def test_metallb_speaker_health(m):
    rows = [
        {"Namespace": "metallb-system", "Names": "speaker-aaa", "Ready": "1/1"},
        {"Namespace": "metallb-system", "Names": "speaker-bbb", "Ready": "0/1"},
        {"Namespace": "spatium", "Names": "api-x", "Ready": "1/1"},
    ]
    assert m.metallb_speaker_health(rows) == (1, 2)
    assert m.metallb_speaker_health([]) == (0, 0)


# ── §3d sizing invariant ───────────────────────────────────────────────────


def _build_state(m, cols: int, rows: int, nodes: int = 1, pods: int = 14):
    st = m.DashboardState({}, _FakeTail(), "/dev/tty1")
    st.role = "control-plane"
    st.cpu_percent = 40.0
    st.ram_used_gib, st.ram_total_gib, st.ram_percent = 2.0, 8.0, 25.0
    st.load_avg = (0.5, 0.4, 0.3)
    st.disks = [("/", 1.0, 8.0, 12.0), ("/var", 5.0, 15.0, 33.0)]
    st.ip_v4 = ["192.168.0.50"]
    st.ip_v6 = []
    st.k3s_state = ("v1.35 · ready", "bold green")
    st.nodes = [
        {"name": f"n{i}", "ready": "True", "roles": "server", "version": "v1.35"}
        for i in range(nodes)
    ]
    st.ps_rows = [
        {
            "Names": f"spatium/pod-{i}",
            "Namespace": "spatium",
            "K8sState": "Running",
            "Status": "Running",
            "State": "Running",
            "Ready": "1/1",
            "Restarts": "0",
            "Node": "n0",
        }
        for i in range(pods)
    ]
    st.term_size = (cols, rows)
    return st


@pytest.mark.parametrize(
    "cols,rows",
    [(213, 52), (120, 30), (80, 24), (80, 20)],
)
def test_render_frame_sizing_invariant(m, cols, rows):
    """render_frame renders at every tier without an exception and ALWAYS
    keeps the vitals row (CPU/RAM) + the F-key footer on-screen, with nothing
    clipped past the terminal height (§3d)."""
    state = _build_state(m, cols, rows)
    console = Console(width=cols, height=rows, file=io.StringIO(), color_system="standard")
    console.print(m.render_frame(state, 1.5))
    out = console.file.getvalue()
    plain = _ANSI.sub("", out)
    assert "CPU" in plain and "RAM" in plain, "vitals row missing"
    assert "F1" in plain and "F5" in plain, "footer F-keys missing"
    lines = plain.split("\n")
    last = max((i for i, ln in enumerate(lines) if ln.strip()), default=0)
    assert last < rows, "content clipped past the terminal height"


def test_render_frame_no_unsafe_glyphs(m):
    """No 256-glyph-console-unsafe characters leak into the rendered frame."""
    state = _build_state(m, 120, 40)
    console = Console(width=120, height=40, file=io.StringIO(), color_system="standard")
    console.print(m.render_frame(state, 1.5))
    bad = re.findall(r"[✓◐○▁-▇▓]", console.file.getvalue())
    assert not bad, f"unsafe glyphs in render: {sorted(set(bad))}"


# ── Pods panel shows the whole cluster (#592) ────────────────────────────────
#
# On a 3-node HA appliance each node's console showed only that node's pods,
# while ``kubectl get pods -A`` showed all of them. An auto-filter kicked in
# above 20 visible pods and kept "problem pods + pods on THIS node"; a 3-node
# appliance idles at ~22, so the filter was effectively always on for exactly
# the deployment where cluster-wide visibility matters most.
#
# It protected nothing either: render_services sorts by _pod_sort_priority
# BEFORE the height clamp, so broken pods float to the top and cannot be
# clipped, and the remainder becomes the "… +N more pods · F3 to list"
# subtitle. Sorting + overflow already do the filter's stated job.


class _StubTail:
    """JournalTail stand-in — render_log only reads ``.lines``."""

    lines: list[str] = []

    def stop(self) -> None:  # pragma: no cover - never called here
        pass


def _pod592(name: str, node: str, state: str = "Running", ready: str = "1/1") -> dict:
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
    """~22 visible pods over three nodes — what a real 3-node appliance idles
    at, and what tripped the old 20-pod threshold."""
    rows = [
        _pod592(f"workload-{node}-{i}", node) for node in ("ddi1", "ddi2", "ddi3") for i in range(7)
    ]
    rows.append(_pod592("migrate-abc", "ddi3", state="Succeeded", ready="0/1"))
    return rows


def _panel_text(m, rows: list[dict], cap: int | None = None) -> str:
    buf = Console(width=200, record=True, file=io.StringIO())
    buf.print(m.render_services("control-plane", rows, cap=cap))
    return _ANSI.sub("", buf.export_text())


def _frame_text(m, rows: list[dict]) -> str:
    """Render the WHOLE frame. This is the path that carried the bug: the
    filter lived in render_frame's caller-side helper, not in render_services,
    so a panel-only test cannot see the regression."""
    state = m.DashboardState(env={}, tail=_StubTail(), tty_path="/dev/tty1")
    state.ps_rows = rows
    buf = Console(width=220, height=70, record=True, file=io.StringIO())
    buf.print(m.render_frame(state, 0.0))
    return _ANSI.sub("", buf.export_text())


def test_frame_shows_pods_from_nodes_other_than_this_one(m) -> None:
    """REGRESSION (#592). The old filter kept pods whose Node == this host.
    None of the fixture's nodes are named after the machine running the test,
    so the pre-fix code renders ZERO workload pods here — the same defect that,
    on a real ddi1, rendered only ddi1's third of the cluster.

    Asserting "at least two distinct nodes" rather than all three: 21 pods
    exceed the panel height, so the last node legitimately overflows into the
    "+N more pods" subtitle. That is the clamp working, not a filter.
    """
    text = _frame_text(m, _idle_three_node_cluster())
    seen = [n for n in ("ddi1", "ddi2", "ddi3") if f"workload-{n}-0" in text]
    assert len(seen) >= 2, f"only saw pods from {seen or 'no nodes'}"
    assert "filtered:" not in text, "the node-local filter label is back"


def test_frame_still_reports_overflow(m) -> None:
    """The subtitle is the operator's cue that the panel is truncated — it is
    what makes dropping the filter safe."""
    text = _frame_text(m, _idle_three_node_cluster())
    assert "more pods" in text
    assert "F3 to list" in text


def test_no_node_local_filter_survives(m) -> None:
    """Gone, not merely re-tuned. A higher threshold would reintroduce the bug
    on a 5- or 7-node cluster."""
    assert not hasattr(m, "_filtered_pods")
    assert not hasattr(m, "_POD_FILTER_THRESHOLD")


def test_completed_jobs_still_hidden(m) -> None:
    """Dropping the node filter must not drop the Succeeded-Job filter — that
    one is load-bearing for panel height (see visible_pods)."""
    assert "migrate-abc" not in _panel_text(m, _idle_three_node_cluster())


def test_problem_pods_sort_above_healthy_ones(m) -> None:
    """The mechanism that actually protects a crash-looping pod."""
    assert m._pod_sort_priority(_pod592("boom", "ddi3", "CrashLoopBackOff", "0/1")) == 0
    assert m._pod_sort_priority(_pod592("wait", "ddi2", "Pending", "0/1")) == 1
    assert m._pod_sort_priority(_pod592("half", "ddi2", "Running", "1/2")) == 2
    assert m._pod_sort_priority(_pod592("fine", "ddi1")) == 3


def test_a_crashloop_on_a_remote_node_survives_a_brutal_clamp(m) -> None:
    """The filter's stated purpose was stopping a crash-looping pod being
    buried under healthy ones. Sort + clamp already guarantee that — even when
    the broken pod is on a REMOTE node and the panel can show one row."""
    rows = _idle_three_node_cluster()
    rows.append(_pod592("boom", "ddi3", state="CrashLoopBackOff", ready="0/1"))
    text = _panel_text(m, rows, cap=1)
    assert "boom" in text, "the crash-looping pod was clipped by the height clamp"
    assert "workload-ddi1-0" not in text, "a healthy pod outranked the crashloop"


def test_clamped_rows_are_reported_not_silently_dropped(m) -> None:
    rows = _idle_three_node_cluster()
    text = _panel_text(m, rows, cap=3)
    # 21 visible (the Succeeded job is filtered out), 3 shown → 18 hidden.
    assert "+18 more pods" in text
    assert "F3 to list" in text
