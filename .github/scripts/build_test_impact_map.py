#!/usr/bin/env python3
"""Build the test-impact map from the shards' coverage data (#1020).

On a push to ``main`` every ``Backend — Tests`` shard runs with
``--cov=app --cov-context=test``, so its ``.coverage`` file records, per line,
WHICH test executed it. The aggregator hands all of those files to this
script, which folds them into one JSON document:

    {
      "schema": 1,
      "sha": "<main commit the shards ran>",
      "files": {"app/services/x.py": ["tests/test_a.py", "tests/test_b.py"], ...},
      "test_files": ["tests/test_a.py", ...]
    }

``files[<app file>]`` is every test FILE that executed at least one line of
that app file inside a test. Lines executed only at import time carry the
empty context and are deliberately NOT attributed: an app file whose list
is empty was only ever imported, never exercised, and the selector treats
that as "cannot say → run everything".

Why file granularity: a node-id list for 4,000 tests blows through ARG_MAX,
and a whole test file is the unit the shards are balanced in anyway.

Needs ``coverage`` (the aggregator installs it); everything else is stdlib.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import coverage

SCHEMA = 1


def _relativize(path: str) -> str:
    """``/home/runner/work/x/x/backend/app/y.py`` → ``app/y.py``.

    ``[tool.coverage.run] relative_files = true`` already makes the stored
    paths relative; this only catches a data file recorded without it.
    """
    marker = "/backend/"
    if path.startswith("app/"):
        return path
    idx = path.rfind(marker)
    return path[idx + len(marker) :] if idx >= 0 else path


def build_map(coverage_files: list[Path]) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        combined = coverage.CoverageData(basename=str(Path(tmp) / ".coverage"))
        for path in coverage_files:
            part = coverage.CoverageData(basename=str(path))
            part.read()
            combined.update(part)

        files: dict[str, list[str]] = {}
        all_tests: set[str] = set()
        for measured in combined.measured_files():
            contexts: set[str] = set()
            for ctxs in combined.contexts_by_lineno(measured).values():
                contexts.update(ctxs)
            # pytest-cov context: ``tests/test_x.py::test_name|run`` (also
            # ``|setup`` / ``|teardown``); "" is import-time / collection.
            tests = sorted({ctx.split("::", 1)[0] for ctx in contexts if ctx and "::" in ctx})
            files[_relativize(measured)] = tests
            all_tests.update(tests)

    return {"schema": SCHEMA, "files": files, "test_files": sorted(all_tests)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("coverage_files", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--sha", default="", help="commit the shards ran, recorded for the log")
    args = parser.parse_args(argv)

    missing = [str(p) for p in args.coverage_files if not p.is_file()]
    if missing:
        print(f"::error::coverage file(s) missing: {missing}", file=sys.stderr)
        return 1

    result = build_map(args.coverage_files)
    result["sha"] = args.sha
    attributed = sum(1 for tests in result["files"].values() if tests)  # type: ignore[union-attr]
    if not result["test_files"]:
        # Coverage ran without ``--cov-context=test``: every line is in the
        # empty context and the map would make the selector run everything
        # forever while looking healthy. Refuse to publish it.
        print(
            "::error::no test contexts in coverage data — was --cov-context=test set?",
            file=sys.stderr,
        )
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, sort_keys=True, indent=1) + "\n")
    print(
        f"test-impact map → {args.out}: {len(result['files'])} app files "  # type: ignore[arg-type]
        f"({attributed} attributed to tests), {len(result['test_files'])} test files"  # type: ignore[arg-type]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
