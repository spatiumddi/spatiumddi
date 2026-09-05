"""The #983 pod-posture gate actually fails when it should.

``.github/scripts/chart-pod-posture.py`` is the guard that stops a NEW
workload shipping without a seccomp profile or a PriorityClass. It fails
OPEN by construction — a bug that makes it skip a workload reports "OK" and
the defect ships — so every assertion here is paired with a negative
control that must FAIL, per the lesson recorded on the #861 harness work.

Lives in ``appliance/tests`` because that is the hermetic pytest job with
PyYAML that runs unconditionally on every PR; the gate itself runs inside
the Charts job, which has helm and kubeconform but no pytest. The alternative
was a fourth test root for one file.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_chart_pod_posture.py -v
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent.parent / ".github" / "scripts" / "chart-pod-posture.py"
)

GOOD_DEPLOYMENT = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      priorityClassName: spatium-control-plane
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: api
          image: example:1
"""

GOOD_CNPG = """
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg
spec:
  instances: 1
  priorityClassName: spatium-control-plane
  seccompProfile:
    type: RuntimeDefault
"""


def _run(manifest: str, tmp_path: Path, *flags: str) -> subprocess.CompletedProcess:
    f = tmp_path / "render.yaml"
    f.write_text(textwrap.dedent(manifest))
    return subprocess.run(
        [sys.executable, str(SCRIPT), *flags, str(f)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"{SCRIPT} missing — the Charts job calls it by path"


def test_a_correct_workload_passes(tmp_path: Path) -> None:
    r = _run(GOOD_DEPLOYMENT, tmp_path, "--require-priority")
    assert r.returncode == 0, r.stderr
    assert "1 workload(s) OK" in r.stdout


def test_missing_seccomp_fails(tmp_path: Path) -> None:
    bad = GOOD_DEPLOYMENT.replace(
        "      securityContext:\n        seccompProfile:\n          type: RuntimeDefault\n",
        "",
    )
    assert "seccompProfile" not in bad, "fixture edit missed — the test would be vacuous"
    r = _run(bad, tmp_path)
    assert r.returncode == 1, r.stdout
    assert "seccompProfile" in r.stderr


def test_missing_priority_fails_only_when_required(tmp_path: Path) -> None:
    bad = GOOD_DEPLOYMENT.replace("      priorityClassName: spatium-control-plane\n", "")
    assert "priorityClassName" not in bad
    # The umbrella chart's default is deliberately no class at all.
    assert _run(bad, tmp_path).returncode == 0
    r = _run(bad, tmp_path, "--require-priority")
    assert r.returncode == 1
    assert "no priorityClassName" in r.stderr


def test_an_empty_priority_class_is_not_a_class(tmp_path: Path) -> None:
    """``priorityClassName: ""`` renders when a values path resolves empty.
    It must not read as "set" — that is precisely the silent-nothing case
    this gate exists to catch."""
    bad = GOOD_DEPLOYMENT.replace(
        "priorityClassName: spatium-control-plane", 'priorityClassName: ""'
    )
    assert _run(bad, tmp_path, "--require-priority").returncode == 1


def test_exemption_records_a_deliberate_priority_zero(tmp_path: Path) -> None:
    bad = GOOD_DEPLOYMENT.replace("      priorityClassName: spatium-control-plane\n", "")
    r = _run(bad, tmp_path, "--require-priority", "--allow-no-priority", "api")
    assert r.returncode == 0, r.stderr
    # ...and the exemption is by name, not a blanket switch.
    r2 = _run(bad, tmp_path, "--require-priority", "--allow-no-priority", "something-else")
    assert r2.returncode == 1


def test_every_pod_bearing_kind_is_inspected(tmp_path: Path) -> None:
    """A workload kind the script does not know about is skipped silently,
    so the set of kinds it walks is itself part of the guard."""
    for kind in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        bad = GOOD_DEPLOYMENT.replace("kind: Deployment", f"kind: {kind}").replace(
            "      securityContext:\n        seccompProfile:\n          type: RuntimeDefault\n",
            "",
        )
        r = _run(bad, tmp_path)
        assert r.returncode == 1, f"{kind} was not inspected: {r.stdout}"


def test_cnpg_cluster_is_checked_through_its_own_fields(tmp_path: Path) -> None:
    """CNPG owns its instance pods, so the properties ride on the CR rather
    than on a pod template. A gate that only knew about pod templates would
    report the database as fine while it ran unranked."""
    assert _run(GOOD_CNPG, tmp_path, "--require-priority").returncode == 0
    without = GOOD_CNPG.replace("  priorityClassName: spatium-control-plane\n", "")
    assert _run(without, tmp_path, "--require-priority").returncode == 1
    without_seccomp = GOOD_CNPG.replace(
        "  seccompProfile:\n    type: RuntimeDefault\n", ""
    )
    assert _run(without_seccomp, tmp_path).returncode == 1


def test_non_workload_documents_are_ignored(tmp_path: Path) -> None:
    """Services, Secrets and the PriorityClasses themselves have no pod
    template; flagging them would make the gate unusable."""
    r = _run(
        """
        apiVersion: v1
        kind: Service
        metadata:
          name: api
        spec:
          ports: [{port: 80}]
        ---
        apiVersion: scheduling.k8s.io/v1
        kind: PriorityClass
        metadata:
          name: spatium-service
        value: 100000
        """,
        tmp_path,
        "--require-priority",
    )
    assert r.returncode == 0, r.stderr
    assert "0 workload(s) OK" in r.stdout


def test_seccomp_exemption(tmp_path: Path) -> None:
    """Used for exactly one thing: a vendored subchart (frr-k8s) whose
    templates expose no pod-securityContext knob, so no values override can
    supply a profile. Exempting it beats not rendering the chart at all."""
    bad = GOOD_DEPLOYMENT.replace(
        "      securityContext:\n        seccompProfile:\n          type: RuntimeDefault\n",
        "",
    )
    assert _run(bad, tmp_path).returncode == 1
    r = _run(bad, tmp_path, "--allow-no-seccomp", "api")
    assert r.returncode == 0, r.stderr
    # ...by name, not a blanket switch.
    assert _run(bad, tmp_path, "--allow-no-seccomp", "other").returncode == 1


def test_exemptions_are_independent(tmp_path: Path) -> None:
    """Exempting seccomp must not quietly exempt the priority check too."""
    bad = GOOD_DEPLOYMENT.replace(
        "      securityContext:\n        seccompProfile:\n          type: RuntimeDefault\n",
        "",
    ).replace("      priorityClassName: spatium-control-plane\n", "")
    r = _run(bad, tmp_path, "--require-priority", "--allow-no-seccomp", "api")
    assert r.returncode == 1
    assert "no priorityClassName" in r.stderr
    assert "seccompProfile" not in r.stderr


def test_exemptions_match_the_release_prefixed_name(tmp_path: Path) -> None:
    """Helm prefixes a subchart's workloads with the release name, so the
    same object is ``frr-k8s`` in the chart and ``metallb-bgp-frr-k8s`` in a
    render. An exact-match-only exemption would stop applying the moment a
    render was renamed — silently, since the gate would then just fail."""
    bad = GOOD_DEPLOYMENT.replace("name: api", "name: metallb-bgp-frr-k8s").replace(
        "      securityContext:\n        seccompProfile:\n          type: RuntimeDefault\n",
        "",
    )
    assert _run(bad, tmp_path, "--allow-no-seccomp", "frr-k8s").returncode == 0
    # An unrelated name must not match — the property that actually matters.
    assert _run(bad, tmp_path, "--allow-no-seccomp", "api").returncode == 1
    # The suffix test is NOT segment-aware, so a short exemption over-matches.
    # Pinned rather than fixed: the docstring tells callers to write full
    # workload names, and an over-broad exemption is still typed by a human
    # into a reviewed file.
    assert _run(bad, tmp_path, "--allow-no-seccomp", "k8s").returncode == 0


def test_exemption_flag_without_a_value_is_a_usage_error(tmp_path: Path) -> None:
    f = tmp_path / "render.yaml"
    f.write_text(GOOD_DEPLOYMENT)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(f), "--allow-no-seccomp"],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 2, r.stdout
