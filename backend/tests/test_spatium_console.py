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
