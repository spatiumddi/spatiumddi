"""The test-impact selection behind #1020 — the rules that may SKIP backend tests on a PR.

Two scripts under ``.github/scripts/``: ``build_test_impact_map.py`` folds the
push-to-main shards' coverage contexts into ``{app file → [test files]}``, and
``select_impacted_tests.py`` applies that map to a PR's changed paths. The
only acceptable failure direction is toward running MORE tests, so every
fail-open rule in the selector's docstring is pinned here, and the map
builder is pinned to refuse data that would make the selector look healthy
while selecting from nothing.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import types

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / ".github" / "scripts"
_SELECT = _SCRIPTS / "select_impacted_tests.py"
_BUILD = _SCRIPTS / "build_test_impact_map.py"
_GATE = _SCRIPTS / "ci-backend-relevant.sh"

pytestmark = pytest.mark.skipif(
    not _SELECT.exists(), reason="needs a full checkout (.github/ above backend/)"
)


def _load(path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def selector() -> types.ModuleType:
    return _load(_SELECT)


@pytest.fixture(scope="module")
def builder() -> types.ModuleType:
    pytest.importorskip("coverage")
    return _load(_BUILD)


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A fake checkout with five test files, so on-disk checks have something to find."""
    tests = tmp_path / "backend" / "tests"
    tests.mkdir(parents=True)
    for name in (
        "test_a.py",
        "test_b.py",
        "test_c.py",
        "test_d.py",
        "test_e.py",
        "conftest.py",
    ):
        (tests / name).write_text("")
    (tmp_path / "backend" / "app" / "services").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "services" / "x.py").write_text("")
    return tmp_path


_MAP: dict[str, object] = {
    "schema": 1,
    "sha": "deadbeef",
    "test_files": ["tests/test_a.py", "tests/test_b.py"],
    "files": {
        "app/services/x.py": ["tests/test_a.py"],
        "app/services/y.py": [
            "tests/test_a.py",
            "tests/test_b.py",
            "tests/test_gone.py",
        ],
        "app/models/m.py": [],
    },
}


def _select(selector: types.ModuleType, repo: pathlib.Path, *changed: str, **kw: object):
    return selector.select(
        list(changed),
        _MAP,
        repo_root=repo,
        gate=_GATE,
        max_fraction=kw.get("max_fraction", 0.6),
    )


def test_a_mapped_service_change_selects_exactly_its_tests(selector, repo) -> None:
    assert _select(selector, repo, "backend/app/services/x.py") == (
        "some",
        ["tests/test_a.py"],
    )


def test_irrelevant_paths_in_the_same_pr_do_not_widen_the_run(selector, repo) -> None:
    """docs/ and frontend/ are the gate's business; a backend PR that also touches them stays narrow."""
    verdict = _select(
        selector,
        repo,
        "backend/app/services/x.py",
        "docs/a.md",
        "frontend/src/x.ts",
        "CHANGELOG.md",
    )
    assert verdict == ("some", ["tests/test_a.py"])


def test_import_only_files_run_everything(selector, repo) -> None:
    """A model's lines execute at import — coverage cannot say which tests depend on it."""
    verdict, why = _select(selector, repo, "backend/app/models/m.py")
    assert verdict == "all"
    assert "import time" in why[0]


def test_a_new_module_adds_nothing_but_its_changed_tests_are_selected(selector, repo) -> None:
    verdict = _select(
        selector, repo, "backend/app/services/brand_new.py", "backend/tests/test_b.py"
    )
    assert verdict == ("some", ["tests/test_b.py"])


@pytest.mark.parametrize(
    "path",
    [
        "backend/tests/conftest.py",
        "backend/tests/helpers.py",
        "backend/alembic/versions/abc_add_column.py",
        "backend/pyproject.toml",
        "backend/Dockerfile",
        "backend/app/data/iana_tlds.json",
        ".github/workflows/ci.yml",
        ".github/scripts/select_impacted_tests.py",
        "appliance/mkosi.extra/usr/local/bin/spatium-console",
        "scripts/export_openapi.py",
        "docs/PRIVACY.md",
    ],
)
def test_anything_outside_the_mapped_surface_runs_everything(selector, repo, path: str) -> None:
    """Migrations, config, data files, test infrastructure and the must-run carve-outs."""
    verdict, _ = _select(selector, repo, "backend/app/services/x.py", path)
    assert verdict == "all", path


def test_a_deleted_test_file_has_nothing_to_run(selector, repo) -> None:
    verdict, why = _select(selector, repo, "backend/tests/test_removed.py")
    assert (verdict, why) == ("all", ["selection is empty"])


def test_map_entries_for_tests_the_pr_removed_are_dropped(selector, repo) -> None:
    """The map is from main; the PR may have renamed a test file it names."""
    assert _select(selector, repo, "backend/app/services/y.py") == (
        "some",
        ["tests/test_a.py", "tests/test_b.py"],
    )


def test_selecting_most_of_the_suite_just_runs_all_of_it(selector, repo) -> None:
    verdict, why = _select(selector, repo, "backend/app/services/y.py", max_fraction=0.3)
    assert verdict == "all"
    assert "over 30%" in why[0]


def test_no_map_or_wrong_schema_runs_everything(selector, repo, tmp_path: pathlib.Path) -> None:
    assert selector.load_map(None) is None
    assert selector.load_map(tmp_path / "missing.json") is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema": 99, "files": {}}))
    assert selector.load_map(wrong) is None
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json")
    assert selector.load_map(garbage) is None
    verdict, why = selector.select(
        ["backend/app/services/x.py"],
        None,
        repo_root=repo,
        gate=_GATE,
        max_fraction=0.6,
    )
    assert (verdict, why) == ("all", ["no usable test-impact map"])


def test_cli_prints_all_or_the_file_list_on_one_line(
    selector, repo, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("backend/app/services/x.py\n")
    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps(_MAP))
    args = [
        "--changed",
        str(changed),
        "--map",
        str(map_file),
        "--repo-root",
        str(repo),
        "--gate",
        str(_GATE),
    ]
    assert selector.main(args) == 0
    assert capsys.readouterr().out == "tests/test_a.py\n"
    assert selector.main(args[:2] + ["--repo-root", str(repo), "--gate", str(_GATE)]) == 0
    assert capsys.readouterr().out == "all\n"


# ── the map builder ──────────────────────────────────────────────────────────


def _coverage_file(
    path: pathlib.Path, lines_by_context: dict[str, dict[str, list[int]]]
) -> pathlib.Path:
    """Write a real coverage data file with the given ``{context: {file: [lines]}}``."""
    import coverage

    path.parent.mkdir(parents=True, exist_ok=True)
    data = coverage.CoverageData(basename=str(path))
    for context, files in lines_by_context.items():
        data.set_context(context)
        data.add_lines(files)
    data.write()
    return path


def test_builder_attributes_files_to_the_tests_that_executed_them(builder, tmp_path) -> None:
    shard1 = _coverage_file(
        tmp_path / "s1" / ".coverage",
        {
            "": {"app/models/m.py": [1, 2], "app/services/x.py": [1]},
            "tests/test_a.py::test_one|run": {"app/services/x.py": [5, 6]},
            "tests/test_a.py::test_one|setup": {"app/services/x.py": [7]},
        },
    )
    shard2 = _coverage_file(
        tmp_path / "s2" / ".coverage",
        {
            "tests/test_b.py::test_two|run": {
                "app/services/x.py": [8],
                "app/services/y.py": [1],
            }
        },
    )
    result = builder.build_map([shard1, shard2])
    assert result["files"] == {
        "app/models/m.py": [],  # import-only → the selector will run everything for it
        "app/services/x.py": ["tests/test_a.py", "tests/test_b.py"],
        "app/services/y.py": ["tests/test_b.py"],
    }
    assert result["test_files"] == ["tests/test_a.py", "tests/test_b.py"]
    assert result["schema"] == 1


def test_builder_relativizes_absolute_runner_paths(builder, tmp_path) -> None:
    shard = _coverage_file(
        tmp_path / ".coverage",
        {
            "tests/test_a.py::t|run": {
                "/home/runner/work/spatiumddi/spatiumddi/backend/app/z.py": [1]
            }
        },
    )
    assert builder.build_map([shard])["files"] == {"app/z.py": ["tests/test_a.py"]}


def test_builder_refuses_data_without_test_contexts(builder, tmp_path, capsys) -> None:
    """Coverage that ran without --cov-context=test would yield a map that selects nothing, forever."""
    shard = _coverage_file(tmp_path / ".coverage", {"": {"app/services/x.py": [1, 2]}})
    out = tmp_path / "map.json"
    assert builder.main([str(shard), "--out", str(out)]) == 1
    assert not out.exists()
    assert "no test contexts" in capsys.readouterr().err


def test_builder_refuses_a_missing_shard_file(builder, tmp_path, capsys) -> None:
    assert builder.main([str(tmp_path / "nope"), "--out", str(tmp_path / "m.json")]) == 1
    assert "missing" in capsys.readouterr().err
