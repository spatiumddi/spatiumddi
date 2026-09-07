"""The shard-durations merge behind #1019 (``scripts/merge_test_durations.py``).

The CI shards are balanced by the committed ``backend/.test_durations``; each
shard uploads only what it measured, and the aggregator runs this script to
fold the pieces back into one file. A bug here has a quiet failure mode — a
merged file missing half the suite, or one that bakes a slow runner's
afternoon into every test that runner happened to hold, would be committed at
the next release prep and silently un-balance the shards again — so the merge
semantics are pinned rather than trusted.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import types

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "merge_test_durations.py"
_COMMITTED = pathlib.Path(__file__).resolve().parents[1] / ".test_durations"

# Inside the dev api container ``backend/`` is the image root and there is no
# repo-level ``scripts/`` above it, so these skip there and run for real in
# CI, which tests from a full checkout (the test_openapi_export.py pattern).
pytestmark = pytest.mark.skipif(
    not _SCRIPT.exists(), reason="needs a full checkout (scripts/ above backend/)"
)


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("merge_test_durations", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def merge_mod() -> types.ModuleType:
    return _load_script()


def _shard(tmp_path: pathlib.Path, name: str, data: object) -> pathlib.Path:
    """Lay a shard file out the way download-artifact does: ``<artifact>/.test_durations``."""
    path = tmp_path / name / ".test_durations"
    path.parent.mkdir()
    path.write_text(json.dumps(data))
    return path


def _committed(tmp_path: pathlib.Path, data: dict[str, float]) -> pathlib.Path:
    path = tmp_path / "committed.json"
    path.write_text(json.dumps(data))
    return path


def _run(
    merge_mod, *shards: pathlib.Path, out: pathlib.Path, committed: pathlib.Path | None = None
):
    args = [str(s) for s in shards] + ["--out", str(out)]
    if committed is not None:
        args += ["--committed", str(committed)]
    return merge_mod.main(args)


def test_without_a_committed_file_the_union_is_taken_as_measured(
    merge_mod, tmp_path: pathlib.Path
) -> None:
    a = _shard(tmp_path, "test-durations-shard-1", {"tests/t_a.py::test_1": 1.5})
    b = _shard(tmp_path, "test-durations-shard-2", {"tests/t_b.py::test_2": 2.5})
    out = tmp_path / "merged" / ".test_durations"

    assert _run(merge_mod, a, b, out=out) == 0

    merged = json.loads(out.read_text())
    assert merged == {"tests/t_a.py::test_1": 1.5, "tests/t_b.py::test_2": 2.5}
    # Byte-for-byte the shape pytest-split itself writes, so a refresh diffs cleanly.
    assert out.read_text() == json.dumps(merged, sort_keys=True, indent=4) + "\n"


def test_a_uniformly_slow_runner_is_normalized_away(
    merge_mod, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Shard 2's runner took exactly 2x on every test: the file must not learn that."""
    committed = _committed(
        tmp_path, {"tests/a.py::t1": 1.0, "tests/a.py::t2": 3.0, "tests/b.py::t3": 2.0}
    )
    fast = _shard(
        tmp_path, "test-durations-shard-1", {"tests/a.py::t1": 1.0, "tests/a.py::t2": 3.0}
    )
    slow = _shard(tmp_path, "test-durations-shard-2", {"tests/b.py::t3": 4.0})
    out = tmp_path / "out.json"

    assert _run(merge_mod, fast, slow, out=out, committed=committed) == 0

    merged = json.loads(out.read_text())
    assert merged == {"tests/a.py::t1": 1.0, "tests/a.py::t2": 3.0, "tests/b.py::t3": 2.0}
    text = capsys.readouterr().out
    assert "runner 2.00x expected" in text
    assert "0.0% of the suite's cost moved" in text
    assert "::warning::" not in text


def test_a_genuine_relative_change_is_blended_in_and_reported(
    merge_mod, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One test got 9x slower on a runner that is otherwise on pace: half of it lands now."""
    committed = _committed(
        tmp_path, {"tests/a.py::fast": 1.0, "tests/a.py::grew": 1.0, "tests/a.py::other": 1.0}
    )
    shard = _shard(
        tmp_path,
        "test-durations-shard-1",
        {"tests/a.py::fast": 1.0, "tests/a.py::grew": 9.0, "tests/a.py::other": 1.0},
    )
    out = tmp_path / "out.json"
    assert _run(merge_mod, shard, out=out, committed=committed) == 0

    merged = json.loads(out.read_text())
    # measured/predicted = 11/3 → normalized: fast 0.27, grew 2.45, other 0.27; blended 50/50.
    assert merged["tests/a.py::grew"] > merged["tests/a.py::fast"] * 2
    assert merged["tests/a.py::grew"] < 9.0  # blended, not replaced
    assert "::warning::backend/.test_durations no longer describes" in capsys.readouterr().out


def test_tests_in_no_shard_are_dropped_and_counted(
    merge_mod, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test in the committed file but in no shard was deleted or renamed — it leaves."""
    committed = _committed(tmp_path, {"tests/t_a.py::test_1": 1.0, "tests/gone.py::test_old": 9.0})
    a = _shard(tmp_path, "test-durations-shard-1", {"tests/t_a.py::test_1": 1.0})
    out = tmp_path / "out.json"

    assert _run(merge_mod, a, out=out, committed=committed) == 0

    assert json.loads(out.read_text()) == {"tests/t_a.py::test_1": 1.0}
    assert "0 tests added, 1 removed" in capsys.readouterr().out


def test_mostly_unknown_tests_warn_and_take_the_measurement_as_is(
    merge_mod, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shard the committed file barely knows gets factor 1.0 (a guess) and a refresh nudge."""
    committed = _committed(tmp_path, {"tests/a.py::t1": 1.0})
    shard = _shard(
        tmp_path,
        "test-durations-shard-1",
        {"tests/a.py::t1": 1.0, "tests/new.py::n1": 5.0, "tests/new.py::n2": 5.0},
    )
    out = tmp_path / "out.json"
    assert _run(merge_mod, shard, out=out, committed=committed) == 0
    merged = json.loads(out.read_text())
    assert merged["tests/new.py::n1"] == 5.0
    text = capsys.readouterr().out
    assert "runner speed unknown" in text
    assert "2 tests added" in text
    assert "::warning::" in text


@pytest.mark.parametrize(
    "payload",
    [
        {},  # a shard that measured nothing is a broken run, not an empty slice
        [],  # pytest-split's pre-v0.5 list shape is not accepted from a shard
        {"tests/a.py::t": -1},
        {"tests/a.py::t": "fast"},
    ],
)
def test_a_malformed_shard_refuses_rather_than_publishing_a_partial_file(
    merge_mod, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], payload: object
) -> None:
    good = _shard(tmp_path, "test-durations-shard-1", {"tests/a.py::t": 1.0})
    bad = _shard(tmp_path, "test-durations-shard-2", payload)
    out = tmp_path / "out.json"

    assert _run(merge_mod, good, bad, out=out) == 1

    assert not out.exists()
    assert "::error::" in capsys.readouterr().err


def test_a_test_in_two_shards_keeps_the_larger_measurement(
    merge_mod, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cannot happen with a correct split; if it does, do not silently halve a slow test."""
    a = _shard(tmp_path, "test-durations-shard-1", {"tests/a.py::t": 2.0})
    b = _shard(tmp_path, "test-durations-shard-2", {"tests/a.py::t": 5.0})
    out = tmp_path / "out.json"
    assert _run(merge_mod, a, b, out=out) == 0
    assert json.loads(out.read_text()) == {"tests/a.py::t": 5.0}
    assert "more than one shard" in capsys.readouterr().err


def test_committed_durations_file_is_a_valid_split_input() -> None:
    """The file the CI shards are balanced with must load the way pytest-split loads it.

    Deliberately NOT asserting that every key names a test that still exists:
    the refresh is a release-prep chore and a renamed test between refreshes
    only costs balance (pytest-split assumes the average for an unknown id).
    """
    assert _COMMITTED.exists(), "backend/.test_durations is missing — CI shards fall back to count"
    data = json.loads(_COMMITTED.read_text())
    assert isinstance(data, dict) and len(data) > 1000, "expected the whole suite, not a slice"
    for key, value in data.items():
        assert key.startswith("tests/") and "::" in key, key
        assert isinstance(value, (int, float)) and value >= 0, (key, value)
