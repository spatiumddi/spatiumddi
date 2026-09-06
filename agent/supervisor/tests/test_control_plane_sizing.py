"""firstboot's control-plane sizing matches the supervisor's (#1003 item 4).

firstboot used to render the chart with the values-file defaults (api 512Mi)
and the supervisor's first heartbeat re-rendered it sized to the node — so
every install paid for a second helm-install Job, a second migrate Job and an
api + worker rollout, three minutes into a box that was already up.

firstboot now computes the same numbers up front. That means the formula
exists twice, in bash and in Python, which is a drift risk — so this test
runs BOTH and requires them to agree. Same pattern as
``test_role_chart_values.py``, which pins the same script's role-chart values
to the Python (#992).

The bash is executed, not pattern-matched: integer division in shell truncates
where Python's ``int(x * 0.5)`` may not, and the clamps are written as
different expressions in the two languages. Only running them finds that.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from spatium_supervisor import k8s_api


def _firstboot() -> Path:
    """Locate spatiumddi-firstboot by walking up to the repo root.

    Deliberately RAISES rather than skipping when it cannot be found. A
    cross-repo-boundary test that quietly skips is one that reports a clean
    pass while checking nothing — and this is the only thing standing between
    the two copies of the sizing formula.
    """
    rel = "appliance/mkosi.extra/usr/local/bin/spatiumddi-firstboot"
    for parent in Path(__file__).resolve().parents:
        candidate = parent / rel
        if candidate.is_file():
            return candidate
    raise AssertionError(
        f"could not find {rel} above {__file__} — this test pins firstboot's "
        f"sizing arithmetic to k8s_api.control_plane_resources and cannot "
        f"run without both"
    )


FIRSTBOOT = _firstboot()

# Real appliance sizes plus the clamp boundaries on both sides.
_SIZES = [
    1024,      # tiny — below every floor
    2048,
    3943,      # the 4 GiB Proxmox VM in the #1003 report
    8192,
    12288,     # _WORKER_SMALL_NODE_MIB exactly (concurrency boundary)
    12289,     # one MiB over it
    16384,
    32768,
    65536,     # api clamp ceiling
    262144,    # far past every ceiling
]


def _bash_sizing(mem_mib: int) -> dict[str, int]:
    """Run firstboot's own arithmetic for a given MemTotal."""
    src = FIRSTBOOT.read_text(encoding="utf-8")
    # Indentation-tolerant: the helper lives inside
    # _render_control_helmchart, so it is indented four spaces. A
    # ``^_clamp_mib`` anchor silently matched nothing when it moved there,
    # and every case failed at once rather than the formula being wrong.
    clamp = re.search(r"^[ \t]*_clamp_mib\(\) \{.*?^[ \t]*\}", src, re.DOTALL | re.MULTILINE)
    assert clamp, "firstboot no longer defines _clamp_mib"
    script = f"""
        set -euo pipefail
        {textwrap.dedent(clamp.group(0))}
        MEM_TOTAL_MIB={mem_mib}
        API_MEM_MIB=$(_clamp_mib "$MEM_TOTAL_MIB" 1 2 1024 8192)
        WORKER_MEM_MIB=$(_clamp_mib "$MEM_TOTAL_MIB" 1 4 1024 4096)
        if [ "$MEM_TOTAL_MIB" -le 12288 ]; then C=2; else C=4; fi
        echo "$API_MEM_MIB $WORKER_MEM_MIB $C"
    """
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout.split()
    return {"api": int(out[0]), "worker": int(out[1]), "concurrency": int(out[2])}


@pytest.mark.parametrize("mem_mib", _SIZES)
def test_bash_and_python_agree(mem_mib: int) -> None:
    py = k8s_api.control_plane_resources(mem_mib)
    sh = _bash_sizing(mem_mib)
    assert py["api"]["resources"]["limits"]["memory"] == f"{sh['api']}Mi"
    assert py["worker"]["resources"]["limits"]["memory"] == f"{sh['worker']}Mi"
    assert py["worker"]["concurrency"] == sh["concurrency"]


def test_firstboot_emits_nothing_when_memtotal_is_unreadable() -> None:
    """A blank fragment leaves the chart's defaults, not `memory: Mi`."""
    src = FIRSTBOOT.read_text(encoding="utf-8")
    assert 'API_SIZING_YAML=""' in src
    assert 'if [ -n "$API_MEM_MIB" ]; then' in src


def test_the_fragments_are_actually_rendered() -> None:
    """Computed but never interpolated is the failure this is prone to."""
    src = FIRSTBOOT.read_text(encoding="utf-8")
    assert "${API_SIZING_YAML}" in src
    assert "${WORKER_SIZING_YAML}" in src


def test_python_still_returns_empty_for_unknown_size() -> None:
    """The contract firstboot's empty-fragment branch mirrors."""
    assert k8s_api.control_plane_resources(None) == {}
    assert k8s_api.control_plane_resources(0) == {}
