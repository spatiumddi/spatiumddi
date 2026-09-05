"""firstboot's rendered manifests carry the #983 posture bits.

Two things ``spatiumddi-firstboot`` emits that nothing else can:

  * the ``spatium`` Namespace's Pod Security Admission labels — the chart
    deliberately does NOT own the namespace (a chart-owned namespace is what
    ``spatiumddi-helm-stuck-recover`` exists to clean up after), so the
    labels can only come from here;

  * ``global.priorityClassName`` in the ``spatium-control`` HelmChart's
    values, which is what makes the umbrella chart's control-plane
    workloads pick up the ``spatium-control-plane`` class that the
    spatiumddi-appliance chart renders.

The second one has a hazard worth a test of its own: a pod naming a
PriorityClass that does not exist is REFUSED by the apiserver — the
Deployment is accepted and the ReplicaSet controller then cannot create
pods. So the override is emitted only when the chart that defines the class
is also going into the auto-deploy dir, and the negative case is asserted
here rather than reasoned about.

Both renderers sit BELOW the ``SPATIUM_FIRSTBOOT_LIB`` early-return, so
they cannot be reached by sourcing the script the way
``test_firstboot_member_guard.py`` does. They are extracted by name and
evaluated on their own instead; ``_extract_function`` fails loudly if the
name is not found, so a rename shows up as an error rather than as a
vacuously passing test.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_firstboot_pod_posture.py -v
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = (
    Path(__file__).parent.parent
    / "mkosi.extra"
    / "usr"
    / "local"
    / "bin"
    / "spatiumddi-firstboot"
)


def _extract_function(name: str) -> str:
    """Return the shell source of a top-level ``name() { ... }`` function.

    Brace-matched from the opening line to a closing ``}`` at column 0,
    which is how every function in this script is written.
    """
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    opener = f"{name}() {{"
    for i, line in enumerate(lines):
        if line == opener:
            break
    else:  # pragma: no cover - the assert below is the real reporter
        raise AssertionError(f"{name}() not found in {SCRIPT} (renamed?)")
    for j in range(i + 1, len(lines)):
        if lines[j] == "}":
            return "\n".join(lines[i : j + 1])
    raise AssertionError(f"{name}() has no closing brace at column 0")


def _run(func_name: str, env: dict[str, str] | None = None) -> str:
    """Define one extracted function in a fresh shell and call it."""
    body = _extract_function(func_name)
    script = f"{body}\n{func_name}\n"
    proc = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"{func_name} exited {proc.returncode}: {proc.stderr}"
    assert proc.stdout.strip(), f"{func_name} rendered nothing"
    return proc.stdout


# ── Namespace ───────────────────────────────────────────────────────────────


def test_namespace_carries_psa_warn_and_audit() -> None:
    doc = yaml.safe_load(_run("_render_namespace_yaml"))
    assert doc["kind"] == "Namespace"
    assert doc["metadata"]["name"] == "spatium"
    labels = doc["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/warn"] == "baseline"
    assert labels["pod-security.kubernetes.io/audit"] == "baseline"


def test_namespace_never_enforces() -> None:
    """``enforce`` would reject the role DaemonSets, the supervisor and the
    hostNetwork frontend — every one of which violates ``baseline`` on
    purpose. Report-only is the only mode this namespace can carry."""
    doc = yaml.safe_load(_run("_render_namespace_yaml"))
    assert "pod-security.kubernetes.io/enforce" not in doc["metadata"]["labels"]


# ── Control-plane HelmChart values ──────────────────────────────────────────


def _control_values(chart_tgz: Path | None) -> dict:
    env = {"CHART_TGZ": str(chart_tgz) if chart_tgz else "/nonexistent/appliance.tgz"}
    doc = yaml.safe_load(_run("_render_control_helmchart", env))
    assert doc["kind"] == "HelmChart"
    return yaml.safe_load(doc["spec"]["valuesContent"])


def test_control_plane_workloads_get_the_priority_class(tmp_path: Path) -> None:
    tgz = tmp_path / "spatiumddi-appliance.tgz"
    tgz.write_bytes(b"not really a chart, only its presence is read")
    values = _control_values(tgz)
    assert values["global"]["priorityClassName"] == "spatium-control-plane"


def test_no_priority_class_when_the_chart_that_defines_it_is_absent() -> None:
    """The class is rendered by the spatiumddi-appliance chart. With that
    chart missing there is nothing to define it, and naming it anyway leaves
    the ReplicaSet controller unable to create a single control-plane pod."""
    values = _control_values(None)
    assert values["global"]["priorityClassName"] == ""


def test_the_node_selector_gate_still_renders(tmp_path: Path) -> None:
    """#272's control-plane node gate shares the ``global`` block the
    priority knob was added to; a bad edit there would drop it silently and
    schedule control-plane pods onto DNS-only nodes (non-negotiable #16)."""
    tgz = tmp_path / "chart.tgz"
    tgz.write_bytes(b"x")
    values = _control_values(tgz)
    assert values["global"]["controlPlaneNodeSelector"] == {
        "spatium.io/role-control-plane": "true"
    }


def test_extractor_reports_a_missing_function() -> None:
    """Negative control for the harness itself: if ``_extract_function``
    quietly returned an empty string for an unknown name, every test above
    would pass against nothing at all."""
    with pytest.raises(AssertionError, match="not found"):
        _extract_function("_render_no_such_thing")
