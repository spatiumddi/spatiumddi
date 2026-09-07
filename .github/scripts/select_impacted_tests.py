#!/usr/bin/env python3
"""Which backend test files can a change set affect? (#1020)

Reads the PR's changed paths and the test-impact map the last ``main`` run
built (``build_test_impact_map.py``), and prints ONE line on stdout: either
``all`` or a space-separated list of test files (``tests/test_x.py``, relative
to ``backend/``) for the shards to run. Reasons go to stderr.

Every rule fails OPEN — toward ``all``. A wrong "run these" is caught by the
push-to-main run, which always runs everything; a wrong "skip that" is not,
so nothing here ever skips a test it cannot account for:

  * no map, an unreadable map, or a map of another schema        → all
  * any changed path the deny-list (``ci-backend-relevant.sh``) does NOT
    call irrelevant and that is not one of the two kinds below  → all
    (migrations, pyproject, Dockerfile, app data files, templates, the
    must-run carve-outs, conftest.py, test helpers, this machinery ...)
  * ``backend/tests/test_*.py`` added or modified                 → that file
  * ``backend/app/**/*.py`` in the map with tests attributed      → those files
  * ``backend/app/**/*.py`` in the map with NO tests attributed    → all
    (only ever executed at import time — a model, a registry — so the
    coverage contexts cannot say who depends on it)
  * ``backend/app/**/*.py`` absent from the map                    → nothing
    (never imported by any test, or brand new: whatever exercises it is
    either a changed test file or reached through a changed module, and
    both of those are already selected)
  * an empty selection, or one covering more than ``--max-fraction`` of the
    suite's test files                                           → all

Selected test files that no longer exist on disk (the map is from ``main``,
the PR may have renamed them) are dropped; the PR's own version of a renamed
test file is selected by the "added or modified" rule.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
HERE = Path(__file__).resolve().parent
DEFAULT_GATE = HERE / "ci-backend-relevant.sh"


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def load_map(path: Path | None) -> dict[str, object] | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        _say(f"impact map unreadable: {exc}")
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or "files" not in data:
        _say("impact map has an unexpected shape")
        return None
    return data


def is_irrelevant(path: str, gate: Path) -> bool:
    """Ask the real deny-list about one path, so this script has no copy of it."""
    result = subprocess.run(
        [str(gate)], input=path + "\n", capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and result.stdout.strip() == "false"


def select(
    changed: list[str],
    impact: dict[str, object] | None,
    *,
    repo_root: Path,
    gate: Path,
    max_fraction: float,
) -> tuple[str, list[str]]:
    """Return ``("all", [reason])`` or ``("some", [test files])``."""
    if impact is None:
        return "all", ["no usable test-impact map"]
    files = impact["files"]
    assert isinstance(files, dict)

    selected: set[str] = set()
    for raw in changed:
        path = raw.strip()
        if not path or is_irrelevant(path, gate):
            continue
        on_disk = (repo_root / path).is_file()

        if path.startswith("backend/tests/"):
            name = Path(path).name
            if name.startswith("test_") and name.endswith(".py"):
                if on_disk:
                    selected.add(path[len("backend/") :])
                continue  # a deleted test file has nothing to run
            return "all", [f"{path}: test infrastructure, not a test file"]

        if path.startswith("backend/app/") and path.endswith(".py"):
            key = path[len("backend/") :]
            if key not in files:
                _say(f"{path}: not in map (new or never imported by a test) — no tests added")
                continue
            tests = files[key]
            if not tests:
                return "all", [f"{path}: only executed at import time, cannot attribute"]
            selected.update(tests)
            continue

        return "all", [f"{path}: outside the mapped surface"]

    kept = sorted(t for t in selected if (repo_root / "backend" / t).is_file())
    if not kept:
        return "all", ["selection is empty"]

    total = len(list((repo_root / "backend" / "tests").rglob("test_*.py"))) or 1
    if len(kept) / total > max_fraction:
        return "all", [
            f"{len(kept)}/{total} test files selected — over {max_fraction:.0%}, run all"
        ]
    return "some", kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--changed", required=True, type=Path, help="one changed path per line")
    parser.add_argument("--map", type=Path, help="test-impact map JSON (absent → all)")
    parser.add_argument("--repo-root", type=Path, default=HERE.parents[1])
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--max-fraction", type=float, default=0.6)
    args = parser.parse_args(argv)

    changed = args.changed.read_text().splitlines()
    verdict, payload = select(
        changed,
        load_map(args.map),
        repo_root=args.repo_root.resolve(),
        gate=args.gate,
        max_fraction=args.max_fraction,
    )
    if verdict == "all":
        _say(f"→ full suite: {'; '.join(payload)}")
        print("all")
    else:
        _say(f"→ {len(payload)} test file(s) selected")
        print(" ".join(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
