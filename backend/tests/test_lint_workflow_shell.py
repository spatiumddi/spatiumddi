"""The workflow shell-status guard (#1036).

``scripts/lint_workflow_shell.py`` refuses ``$?`` captured from a bare command
in a context where ``-e`` is active — which is every GitHub Actions ``run:``
block, because Actions supplies ``bash -e`` and the ``set -uo pipefail`` those
blocks open with does not clear it.

The two cases that matter most, and that the first draft got wrong in opposite
directions, are both pinned below: the real pre-fix ``trivy-scheduled.yml``
body must be REPORTED, and a ``set -e`` sitting inside a function must not be
read as evidence that ``-e`` is on at some arbitrary later line.
"""

from __future__ import annotations

import importlib.util
import pathlib
import textwrap
import types

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "lint_workflow_shell.py"

pytestmark = pytest.mark.skipif(
    not _SCRIPT.exists(),
    reason="workflow shell linter not present in this checkout",
)


@pytest.fixture(scope="module")
def lint() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("lint_workflow_shell", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow(tmp_path: pathlib.Path, body: str, shell: str | None = None) -> pathlib.Path:
    """Write a one-step workflow whose `run:` block is `body`."""
    shell_line = f"        shell: {shell}\n" if shell else ""
    indented = textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 10)
    wf = tmp_path / "workflows"
    wf.mkdir(exist_ok=True)
    (wf / "w.yml").write_text(
        "name: t\non: {push: {}}\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - name: s\n" + shell_line + "        run: |\n" + indented + "\n"
    )
    return wf


def _findings(lint: types.ModuleType, wf: pathlib.Path) -> list:
    return lint.scan_workflows(wf)


# ── the bug this exists for ──────────────────────────────────────────────────


def test_the_real_pre_fix_trivy_body_is_reported(lint: types.ModuleType) -> None:
    """The actual shape that disarmed the weekly CVE scan.

    Trivy exits 1 **on findings**, so `-e` killed the step at the first image
    with a CVE, `has_findings` was never written, and the reporting step's
    fail-safe left the tracking issue alone. A clean week and a week full of
    criticals looked identical from outside.
    """
    findings = lint.scan_block(
        textwrap.dedent("""
            set -uo pipefail
            for spec in "${specs[@]}"; do
              docker run --rm aquasec/trivy:latest image --exit-code 1 "$img" > out.txt
              rc=$?
              if [ "$rc" -eq 0 ]; then echo clean; else any=1; fi
            done
            echo "has_findings=$any" >> "$GITHUB_OUTPUT"
            """),
        "trivy-scheduled.yml",
        1,
        e_by_default=True,
    )
    assert len(findings) == 1
    assert findings[0].line == "rc=$?"


def test_direct_test_of_dollar_question_is_reported(lint: types.ModuleType) -> None:
    findings = lint.scan_block(
        "set -uo pipefail\nmycmd > out\nif [ $? -ne 0 ]; then echo bad; fi\n",
        "w.yml",
        1,
        e_by_default=True,
    )
    assert len(findings) == 1


# ── the three correct forms must all pass ────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("set -uo pipefail\nif mycmd > out; then rc=0; else rc=$?; fi\n", "the if form"),
        ("set -uo pipefail\nmycmd > out || true\nrc=$?\n", "|| true"),
        ("set -uo pipefail\nset +e\nmycmd > out\nrc=$?\nset -e\n", "explicit set +e"),
        ("set -uo pipefail\nmycmd > out  # lint-workflow-shell: allow\nrc=$?\n", "opt-out marker"),
        ("set -uo pipefail\n# lint-workflow-shell: allow\nrc=$?\n", "marker on the line above"),
    ],
)
def test_safe_forms_pass(lint: types.ModuleType, body: str, why: str) -> None:
    assert lint.scan_block(body, "w.yml", 1, e_by_default=True) == [], why


# ── the false positive that made the first draft unusable ────────────────────


def test_set_e_inside_a_function_does_not_arm_the_check(lint: types.ModuleType) -> None:
    """File order is not execution order, and this reads the file.

    ``spatium-install`` is ``set -uo pipefail`` at the top and turns ``-e`` on
    deep inside ``do_install()`` — which runs AFTER the wizard loop and the
    preseed parser that appear further down the file. Counting an indented
    ``set -e`` produced four confident findings about code where ``-e`` is
    off, and a linter that cries wolf on the installer is one that gets
    deleted. Turning the check ON therefore needs a TOP-LEVEL ``set -e``.
    """
    body = textwrap.dedent("""
        set -uo pipefail

        do_install() {
            set -e
            mkfs.ext4 "$dev"
        }

        run_step() {
            "$step"
            rc=$?
        }
        """)
    assert lint.scan_block(body, "spatium-install", 1, e_by_default=False) == []


def test_top_level_set_e_in_a_script_does_arm_the_check(lint: types.ModuleType) -> None:
    """The other half: an unindented `set -e` really has run by then."""
    body = "#!/usr/bin/env bash\nset -euo pipefail\nmycmd > out\nrc=$?\n"
    assert len(lint.scan_block(body, "s.sh", 1, e_by_default=False)) == 1


def test_a_plain_script_without_set_e_is_not_flagged(lint: types.ModuleType) -> None:
    body = "#!/bin/sh\nmycmd > out\nrc=$?\n"
    assert lint.scan_block(body, "s.sh", 1, e_by_default=False) == []


# ── workflow parsing ─────────────────────────────────────────────────────────


def test_run_block_is_found_and_line_numbers_are_real(
    lint: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    """A finding a human cannot locate is a finding nobody fixes."""
    wf = _workflow(tmp_path, "set -uo pipefail\nmycmd\nrc=$?\n")
    findings = _findings(lint, wf)
    assert len(findings) == 1
    reported = (wf / "w.yml").read_text().split("\n")[findings[0].line_no - 1]
    assert reported.strip() == "rc=$?", "the reported line number must point at the capture"


def test_a_non_bash_shell_is_not_scanned(lint: types.ModuleType, tmp_path: pathlib.Path) -> None:
    """`shell: python` has no `-e` and no `$?`."""
    wf = _workflow(tmp_path, "import os\nrc = 0\n", shell="python")
    assert _findings(lint, wf) == []


def test_a_custom_shell_without_e_is_not_flagged(
    lint: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    wf = _workflow(tmp_path, "mycmd\nrc=$?\n", shell="bash --noprofile {0}")
    assert _findings(lint, wf) == []


def test_a_custom_shell_with_e_is_flagged(lint: types.ModuleType, tmp_path: pathlib.Path) -> None:
    wf = _workflow(tmp_path, "mycmd\nrc=$?\n", shell="bash -eo pipefail {0}")
    assert len(_findings(lint, wf)) == 1


def test_empty_workflow_dir_is_refused_not_passed(
    lint: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    """ "0 workflows scanned" must never print the same line as "all clean".

    The failure this whole guard is about is a check that evaluates nothing
    and looks like one that passed (#1028/#1029/#1030); it would be absurd for
    the guard itself to have it.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    assert lint.main(["--workflow-dir", str(empty), "--script-dir", str(tmp_path / "nope")]) == 1


# ── the /code-review findings, each pinned ───────────────────────────────────


def test_or_true_at_the_end_of_a_continued_command_is_seen(lint) -> None:
    r"""The safe suffix and the safe structure live at OPPOSITE ends.

    `else`/`fi` are at the start of the preceding command; `|| true` is at its
    end. Testing only the first physical line of a `\`-continued command
    reported a very common CI shape as a violation — and printed the wrong
    line as the context.
    """
    body = "set -uo pipefail\nmycmd \\\n  --flag \\\n  || true\nrc=$?\n"
    assert lint.scan_block(body, "w.yml", 1, e_by_default=True) == []


def test_a_continuation_without_or_true_is_still_caught(lint) -> None:
    body = "set -uo pipefail\nmycmd \\\n  --flag\nrc=$?\n"
    assert len(lint.scan_block(body, "w.yml", 1, e_by_default=True)) == 1


@pytest.mark.parametrize(
    ("body", "want", "why"),
    [
        ("set +o errexit\ncmd\nrc=$?\n", 0, "`set +o errexit` disarms it"),
        ("set -o errexit\ncmd\nrc=$?\n", 1, "`set -o errexit` arms it"),
    ],
)
def test_long_form_errexit_is_understood(lint, body: str, want: int, why: str) -> None:
    """`-o`/`+o` carry no `e`, and `errexit` carries no sign.

    The first cut matched neither, so `set +o errexit` left the check armed
    over a block that had deliberately turned it off.
    """
    assert len(lint.scan_block(body, "s.sh", 1, e_by_default=False)) == want, why


def test_set_e_can_rearm_inside_a_workflow_block(lint, tmp_path) -> None:
    """Run-block bodies must be DEDENTED before the top-level rule sees them.

    Every line of a YAML block scalar carries the block's indent, so nothing
    in a `run:` block was ever "top level" and `set -e` could never re-arm
    after a `set +e` — a false negative in the one place the check is meant
    to be unconditional.
    """
    wf = _workflow(tmp_path, "set +e\ncmd\nset -e\ncmd2\nrc=$?\n")
    assert len(_findings(lint, wf)) == 1


def test_a_previous_steps_shell_does_not_leak(lint, tmp_path) -> None:
    """`shell:` binds to its own step, not to whatever the window caught."""
    wf = tmp_path / "workflows"
    wf.mkdir(exist_ok=True)
    (wf / "w.yml").write_text(
        "name: t\non: {push: {}}\njobs:\n  j:\n    steps:\n"
        "      - name: a\n        shell: python\n        run: |\n          x = 1\n"
        "      - name: b\n        run: |\n          cmd\n          rc=$?\n"
    )
    assert len(lint.scan_workflows(wf)) == 1, "the bash step must still be checked"


def test_extensionless_shell_files_are_scanned(lint, tmp_path) -> None:
    """The appliance host runners have no extension — a `*.sh` glob saw none.

    All 45 of them, including `spatium-install`, which is the file the
    top-level asymmetry and DEVELOPMENT.md §10 are justified by. The guard
    was reasoning about a file it never opened.
    """
    d = tmp_path / "bin"
    d.mkdir()
    runner = d / "spatiumddi-thing"
    runner.write_text("#!/usr/bin/env bash\nset -euo pipefail\nmycmd\nrc=$?\n")
    findings, seen = lint.scan_scripts([d])
    assert seen == 1, "a shebang must be enough to identify a shell file"
    assert len(findings) == 1


def test_binary_and_non_shell_files_are_skipped(lint, tmp_path) -> None:
    d = tmp_path / "bin"
    d.mkdir()
    (d / "blob").write_bytes(b"\x7fELF\x02\x01\x01\x00rc=$?\n")
    (d / "notes.txt").write_text("rc=$?\n")
    findings, seen = lint.scan_scripts([d])
    assert (findings, seen) == ([], 0)


def test_a_missing_requested_script_dir_is_an_error(lint, tmp_path) -> None:
    """A typo'd --script-dir silently scanned nothing and still passed."""
    wf = _workflow(tmp_path, "echo hi\n")
    assert lint.main(["--workflow-dir", str(wf), "--script-dir", str(tmp_path / "nope")]) == 1
