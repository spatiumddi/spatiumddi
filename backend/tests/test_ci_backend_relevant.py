"""The CI change-detection filter that decides whether THIS suite runs (#813).

``.github/scripts/ci-backend-relevant.sh`` gates the 8 backend test shards. A
bug in it is uniquely bad: it doesn't fail loudly, it makes a PR go green
without having been tested. So the path list gets pinned here rather than
being validated by pushing branches and squinting at Actions.

The circularity is deliberate and safe in the direction that matters. The
script lives under ``.github/``, which is *not* in its own deny-list, so any
edit to it runs the suite that contains this test.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2] / ".github" / "scripts" / "ci-backend-relevant.sh"
)

# The dev container copies only ``backend/`` into the image, so these skip
# there and run for real in CI, which tests from a full checkout. Same
# convention as test_spatium_console.py. A deleted script is caught loudly by
# the workflow step that invokes it, not by a silent skip here.
pytestmark = pytest.mark.skipif(
    not _SCRIPT.exists(),
    reason="CI change-detection script not present in this checkout",
)


def _relevant(*paths: str) -> bool:
    """Run the real script over a synthetic change set."""
    proc = subprocess.run(
        ["bash", str(_SCRIPT)],
        input="".join(f"{p}\n" for p in paths),
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout.strip()
    assert out in ("true", "false"), f"unexpected output {out!r} (stderr: {proc.stderr})"
    return out == "true"


def test_script_is_executable() -> None:
    """The workflow invokes it by path, so the +x bit has to be committed."""
    assert _SCRIPT.stat().st_mode & 0o111, f"{_SCRIPT} is not executable"


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/main.py",
        "backend/tests/test_health.py",
        "backend/alembic/versions/abc123_thing.py",
        "backend/pyproject.toml",
    ],
)
def test_backend_paths_run_the_suite(path: str) -> None:
    assert _relevant(path)


@pytest.mark.parametrize(
    ("path", "why"),
    [
        (
            "appliance/mkosi.extra/usr/local/bin/spatium-console",
            "test_spatium_console.py loads this file directly — 52 tests",
        ),
        (
            "agent/supervisor/spatium_supervisor/firewall_renderer.py",
            "test_appliance_firewall_render.py imports this by path",
        ),
    ],
)
def test_cross_boundary_reads_still_run_the_suite(path: str, why: str) -> None:
    """The two directories that look unrelated but aren't.

    An allow-list of ``backend/**`` would have skipped both, and the loss
    would have been invisible — a green PR with the tests never run.
    """
    assert _relevant(path), why


@pytest.mark.parametrize(
    "path",
    [
        "docs/features/DNS.md",
        "docs/index.md",
        "website/index.html",
        "frontend/src/App.tsx",
        "frontend/package.json",
        "charts/spatiumddi/values.yaml",
        "k8s/base/api.yaml",
        "CHANGELOG.md",
        "README.md",
        "CLAUDE.md",
        "NOTICE",
        "LICENSE",
    ],
)
def test_known_irrelevant_paths_skip_the_suite(path: str) -> None:
    assert not _relevant(path)


def test_one_relevant_file_among_many_irrelevant_ones_runs_the_suite() -> None:
    """The decision is "any", not "most" — a single backend file counts."""
    assert _relevant(
        "docs/features/DNS.md",
        "CHANGELOG.md",
        "frontend/src/App.tsx",
        "backend/app/drivers/dns/bind9.py",
    )


def test_an_all_irrelevant_change_set_skips() -> None:
    assert not _relevant("docs/a.md", "docs/b.md", "frontend/src/x.ts", "CHANGELOG.md")


def test_an_empty_change_set_runs_the_suite() -> None:
    """Empty is not evidence of anything.

    It means the diff failed, or the caller piped nothing — both of which are
    upstream breakage, not "there is nothing to test".
    """
    assert _relevant()


@pytest.mark.parametrize(
    "path",
    [
        "terraform/main.tf",
        "ansible/playbook.yml",
        "perf/locustfile.py",
        "some-new-top-level-thing/x.py",
        "Makefile",
        "docker-compose.yml",
        ".github/workflows/ci.yml",
        ".github/scripts/ci-backend-relevant.sh",
        "scripts/lint_migrations.py",
        ".env.example",
    ],
)
def test_unrecognised_paths_default_to_running(path: str) -> None:
    """The deny-list's whole point.

    Adding a directory nobody thought about must cost runner-minutes, never
    test coverage. If this test starts failing because someone widened the
    deny-list, the question to answer is "can the backend suite really not
    care about this?" — not "how do I make the test pass?".
    """
    assert _relevant(path)


def test_nested_markdown_is_not_blanket_skipped() -> None:
    """Only ROOT-level *.md is deny-listed.

    ``backend/app/README.md`` is unlikely to matter, but the pattern that
    would skip it would also skip a nested .md in a directory that does.
    """
    assert _relevant("backend/some/README.md")


def test_a_file_moved_out_of_backend_still_runs_the_suite() -> None:
    """Both sides of a rename have to reach the filter.

    ``git diff --name-only`` with rename detection ON — the default —
    reports only the DESTINATION, so ``git mv backend/thing.py
    docs/thing.md`` reads as a docs-only change while actually deleting a
    backend file. The workflow passes ``--no-renames`` to get both paths;
    this pins the filter's half of that contract.
    """
    assert _relevant("backend/thing.py", "docs/thing.md")
