"""Every PR-time Trivy lane gates rather than trusting Trivy's exit code (#1093).

Four workflows build an image on a PR and scan it: the DNS, DHCP,
looking-glass and supervisor lanes. Two properties have to hold, and neither
is visible in a green run — a lane with both broken looks exactly like a lane
that passed:

1. **The package layer is busted.** All four cache on ``type=gha``, and
   BuildKit keys a layer on its RUN text, so the image's ``apk upgrade`` /
   ``apt-get upgrade`` is served from whenever the scope was first written.
   Scanning that layer is wrong in both directions: it misses a CVE the
   current index would flag, and it keeps reporting one fixed weeks ago
   (#1029's class). Three of the four passed no snapshot build-arg at all.

2. **The verdict comes from trivy-gate.sh, not ``exit-code: 1``.** Trivy's
   "fixed" means the distro's security database names a fix, not that the
   package is on the mirrors yet. A bare non-zero exit fails the PR on
   findings nothing could have installed — the nightly-20260905 failure,
   relocated to a lane that blocks unrelated work.

Structural, like ``test_trivy_scheduled_report.py``: running these needs
Docker, a vulnerability database and a built image.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

# Globbed, not listed: a fifth PR-time image lane is covered the day it lands
# rather than whenever someone remembers this file. (It also needs an entry in
# ci-backend-must-run.txt so editing it runs these tests -- see the note there.)
_LANES = sorted(p.name for p in _WORKFLOWS.glob("build-*images*.yml"))
_LANES += sorted(p.name for p in _WORKFLOWS.glob("build-*image.yml") if p.name not in _LANES)

pytestmark = pytest.mark.skipif(
    not _WORKFLOWS.is_dir() or not _LANES,
    reason="PR-time image workflows not present in this checkout",
)


def test_the_lane_list_is_not_empty() -> None:
    """A glob that matches nothing would make every test below vacuously pass."""
    assert len(_LANES) >= 4, f"expected the four known lanes, found {_LANES}"


def _steps(lane: str) -> list[dict]:
    doc = yaml.safe_load((_WORKFLOWS / lane).read_text())
    return [s for job in doc["jobs"].values() for s in job.get("steps", [])]


@pytest.mark.parametrize("lane", _LANES)
def test_the_lane_routes_through_the_gate(lane: str) -> None:
    steps = _steps(lane)
    trivy = [s for s in steps if "trivy-action" in str(s.get("uses", ""))]
    assert trivy, f"{lane}: no Trivy step found — did the lane get restructured?"

    for step in trivy:
        exit_code = str(step.get("with", {}).get("exit-code", ""))
        assert exit_code == "0", (
            f"{lane}: Trivy must not decide (exit-code={exit_code!r}); a non-zero exit "
            "stops the job before the gate runs and fails PRs on unmirrored fixes"
        )
        assert step["with"].get("format") == "json", f"{lane}: the gate reads JSON"

    assert any(
        "trivy-gate.sh" in str(s.get("run", "")) for s in steps
    ), f"{lane}: no gate step — Trivy's findings would be reported by nothing"


@pytest.mark.parametrize("lane", _LANES)
def test_the_lane_busts_its_cached_package_layer(lane: str) -> None:
    """A cached `apk upgrade` layer makes the scan describe a past image."""
    steps = _steps(lane)
    builds = [
        s
        for s in steps
        if "build-push-action" in str(s.get("uses", ""))
        and "type=gha" in str(s.get("with", {}).get("cache-from", ""))
    ]
    assert builds, f"{lane}: no gha-cached build step found"
    for step in builds:
        args = str(step.get("with", {}).get("build-args", ""))
        assert "SNAPSHOT=" in args, (
            f"{lane}: a gha-cached build passes no *_SNAPSHOT build-arg, so the "
            "package layer — and therefore what Trivy scans — is frozen"
        )


@pytest.mark.parametrize("lane", _LANES)
def test_the_trivy_action_is_pinned(lane: str) -> None:
    """`@master` is the #1095 class: tooling that can move under us. An
    unrecognised flag makes Trivy exit 1, which a lane could read as findings."""
    for step in _steps(lane):
        uses = str(step.get("uses", ""))
        if "trivy-action" in uses:
            assert not uses.endswith("@master"), f"{lane}: pin the Trivy action, got {uses!r}"


@pytest.mark.parametrize("lane", _LANES)
def test_the_lane_is_triggered_by_changes_to_itself(lane: str) -> None:
    """A change to how a lane builds or scans must be exercised by that lane.

    None of these listed their own workflow file in ``paths:``, so the PR that
    rewired this very gate would have run none of them — the one change most
    needing the proof was the one change that could not get it. Found by
    checking rather than assuming, and ``build-appliance-builder.yml`` had
    already been doing it, so this is the repo's convention, not a new rule.
    """
    doc = yaml.safe_load((_WORKFLOWS / lane).read_text())
    # PyYAML parses a bare ``on:`` key as the boolean True.
    triggers = doc.get(True) or doc.get("on") or {}
    paths = (triggers.get("pull_request") or {}).get("paths") or []
    assert any(lane in entry for entry in paths), (
        f"{lane}: does not list itself in pull_request.paths, so editing it "
        "does not run it — the change ships unexercised"
    )
