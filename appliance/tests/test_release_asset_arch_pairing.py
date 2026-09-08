"""Release assets exist per architecture, and the pruner knows it (#1026).

Two independent things have to agree about which architectures the
project publishes appliance artifacts for:

* ``.github/workflows/release.yml`` — the ``arch`` matrix that BUILDS
  them, and ``nightly.yml``, which must not drift from it;
* ``scripts/prune-release-assets.sh`` — the ``APPLIANCE_ARCHES`` list
  that RECLAIMS them.

Drift between the two is silent and one-directional in the expensive
direction. The pruner's final ``*)`` branch leaves anything it does not
recognise alone, which is the right default for a genuinely new artifact
and exactly wrong for an architecture somebody added to the build
matrix: nothing errors, nothing is logged, and a ~1.6 GB ISO plus a
~1.6 GB slot image accumulate on every release forever — on a repo whose
only reason for having a pruner is that those add up.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_release_asset_arch_pairing.py -v

No Docker, no network, no gh CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
PRUNER = REPO / "scripts" / "prune-release-assets.sh"
RELEASE_WF = REPO / ".github" / "workflows" / "release.yml"
NIGHTLY_WF = REPO / ".github" / "workflows" / "nightly.yml"

PRUNER_SRC = PRUNER.read_text(encoding="utf-8")


def _pruner_arches() -> list[str]:
    m = re.search(r"^APPLIANCE_ARCHES=\(([^)]*)\)", PRUNER_SRC, re.M)
    assert m, "APPLIANCE_ARCHES=() not found in the pruner"
    return sorted(m.group(1).split())


def _matrix_arches(workflow: Path) -> list[str]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        uses = job.get("uses") or ""
        if "build-appliance.yml" not in uses:
            continue
        arches = (job.get("strategy") or {}).get("matrix", {}).get("arch")
        assert arches, f"{workflow.name}: appliance job has no arch matrix"
        return sorted(arches)
    pytest.fail(f"{workflow.name}: no job calls build-appliance.yml")


def test_the_pruner_covers_every_architecture_the_release_builds():
    assert _pruner_arches() == _matrix_arches(RELEASE_WF)


def test_the_nightly_builds_the_same_architectures_as_the_release():
    """The nightly exists so a regression in the assembly surfaces
    overnight rather than when a release is cut (#823). An architecture
    the release builds and the nightly does not is one whose regressions
    are found at exactly the moment #823 was filed to avoid."""
    assert _matrix_arches(NIGHTLY_WF) == _matrix_arches(RELEASE_WF)


def test_both_matrices_are_fail_fast_false():
    """The two architectures are independent artifacts. ``fail-fast``
    would withhold a perfectly good amd64 ISO from a release because the
    arm64 leg broke."""
    for wf in (RELEASE_WF, NIGHTLY_WF):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job in doc["jobs"].values():
            if "build-appliance.yml" not in (job.get("uses") or ""):
                continue
            assert job["strategy"].get("fail-fast") is False, wf.name


def test_the_versioned_sha_is_matched_before_the_catch_all_glob():
    """An ordering trap, and the reason this file exists.

    ``spatiumddi-appliance-slot-*.sha256`` is there to delete the stray
    mkosi-ImageVersion sidecar (``…-slot-0.1.0.sha256``). It also matches
    every REAL ``…-slot-<tag>-<arch>.sha256`` — the provenance sidecar
    #392 keeps deliberately after the heavy assets are gone. Reached
    first, it deletes exactly what the versioned-sha branch above exists
    to protect, on every release, silently.
    """
    sha_branch = PRUNER_SRC.index('_in_list "$a" "${VER_SHA[@]}"')
    glob_branch = PRUNER_SRC.index("spatiumddi-appliance-slot-*.sha256)")
    assert sha_branch < glob_branch, (
        "the versioned-sha keep must be matched BEFORE the stray-sha glob"
    )


def test_the_pruner_still_reclaims_pre_1026_unarched_isos():
    """Releases cut before this change name their ISO
    ``spatiumddi-appliance-<tag>.iso`` with no architecture. Dropping
    that from the heavy list would silently stop reclaiming every ISO
    ever published up to now — the bulk of what the pruner is for."""
    assert 'VER_HEAVY+=("spatiumddi-appliance-${tag}.iso")' in PRUNER_SRC


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_every_asset_shape_is_generated_for_each_architecture(arch: str):
    """The three shapes the release publishes per architecture: the
    stable (latest-link) ISO + slot pair, the versioned heavy pair, and
    the versioned sha sidecar."""
    for shape in (
        'spatiumddi-appliance-${_a}.iso',
        'spatiumddi-appliance-slot-${_a}.raw.xz',
        'spatiumddi-appliance-${tag}-${_a}.iso',
        'spatiumddi-appliance-slot-${tag}-${_a}.raw.xz',
        'spatiumddi-appliance-slot-${tag}-${_a}.sha256',
    ):
        assert shape in PRUNER_SRC, shape
    assert arch in _pruner_arches()
