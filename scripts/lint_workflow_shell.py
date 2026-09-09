#!/usr/bin/env python3
"""Guard against `$?` captured from a bare command under `set -e` (#1036).

GitHub Actions runs a ``run:`` block with ``bash -e`` (the default shell is
``bash -e {0}``; an explicit ``shell: bash`` is ``bash --noprofile --norc -eo
pipefail {0}``). Writing ``set -uo pipefail`` at the top of the script — which
several steps in this repo do — does **not** clear ``-e``::

    $ bash -ec 'set -uo pipefail; case "$-" in *e*) echo "-e STILL ON";; esac'
    -e STILL ON

So this shape is dead code exactly when it matters::

    some_command > out          # <- -e kills the step here when it fails
    rc=$?                       # <- never reached
    if [ "$rc" -eq 0 ]; then ...

**Why this is worth a linter of its own.** The step goes green on the happy
path, and the branch that would have reported a problem is the one that never
runs — so the failure is invisible by construction. It cost this repo a
working weekly CVE scan: ``trivy-scheduled.yml`` captured Trivy's status this
way, and Trivy exits 1 *on findings*, which is the only case the job exists
for. The step died at the first image with a CVE, ``has_findings`` was never
written, and the reporting step's fail-safe correctly declined to touch the
tracking issue. A clean week and a week full of criticals looked identical.

Neither ``actionlint`` nor ``shellcheck -S style`` reports this shape
(verified against both, 2026-09-09) — shellcheck's SC2181 fires on a direct
``if [ $? -ne 0 ]`` but not on ``rc=$?`` followed by a test of ``$rc``. Hence
this script rather than adopting a tool.

**The fix is always one of three**, and each is recognised as safe here:

* the ``if`` form — ``if cmd; then rc=0; else rc=$?; fi`` — whose condition is
  exempt from ``-e``. Preferred: it keeps the status AND the guard.
* ``cmd || true`` on the preceding line, when the status is not needed.
* an explicit ``set +e`` earlier in the same block, when a long run of
  commands must not abort.

A genuine exception can carry ``# lint-workflow-shell: allow`` on the capture
line or the line above, which is deliberately ugly and greppable.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import textwrap
from typing import Iterable, NamedTuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
#: Every tree holding shell this repo ships or runs. The appliance host
#: runners are the ones that matter most and were the ones missed: they are
#: EXTENSION-LESS, so a ``*.sh`` glob saw none of the 45 — including
#: ``spatium-install``, the file the top-level asymmetry above is justified by.
SCRIPT_DIRS = (
    REPO_ROOT / ".github" / "scripts",
    REPO_ROOT / "appliance" / "scripts",
    REPO_ROOT / "appliance" / "mkosi.extra" / "usr" / "local" / "bin",
    REPO_ROOT / "agent",
)

#: A file is shell if it ends in .sh or opens with a sh/bash shebang.
_SHEBANG_RE = re.compile(rb"^#!.*\b(?:ba|da|k|z)?sh\b")

#: ``rc=$?`` / ``local rc=$?`` / ``declare rc=$?`` on a line of its own.
_CAPTURE_RE = re.compile(r"^\s*(?:local\s+|declare\s+|export\s+)?[A-Za-z_][A-Za-z0-9_]*=\$\?\s*(?:#.*)?$")

#: A direct test of ``$?``: ``if [ $? -ne 0 ]``.
_DIRECT_TEST_RE = re.compile(r"^\s*(?:if|while|until)\s+.*\[\[?\s*\"?\$\?")

#: ``set -e`` in any bundled spelling. ``set +e`` is matched separately.
_SET_E_RE = re.compile(r"^\s*set\s+[-+][A-Za-z]*\b")

_ALLOW_MARKER = "lint-workflow-shell: allow"

#: Lines after which capturing ``$?`` is safe, because the preceding command
#: either cannot abort the script or already ran inside an exempt context.
_SAFE_PRECEDING = re.compile(
    r"(?:^|\s)(?:else|fi|then|do|done|\{|\})\s*$"  # if/else/loop structure
    r"|\|\|\s*(?:true|:)\s*$"  # explicitly tolerated failure
    r"|\|\|\s*\\$"  # continued `|| \`
)


class Finding(NamedTuple):
    path: str
    line_no: int
    line: str
    context: str


def _set_e_active(lines: list[str], upto: int, default: bool) -> bool:
    """Is ``-e`` in effect at ``lines[upto]``?

    ``default`` is True for a workflow ``run:`` block (Actions supplies
    ``-e`` on the interpreter command line) and False for a standalone
    ``.sh`` file, which only has it if it asks. ``set -uo pipefail`` does NOT
    clear ``-e``, so only an explicit ``+e`` in the flag string turns it off —
    which is the entire bug this guard exists for.

    **The two directions are deliberately asymmetric**, because file order is
    not execution order and this reads the file:

    * turning ``-e`` ON requires an UNINDENTED ``set -e`` — i.e. one at the
      script's top level, which really has run by the time a later line does.
      An indented one is inside a function or a subshell and says nothing
      about an arbitrary later line. ``spatium-install`` is the worked
      example: it is ``set -uo pipefail`` at the top and enables ``-e`` deep
      inside ``do_install()``, which runs *after* the wizard loop and the
      preseed parser that appear below it in the file. Counting that would
      report four confident findings about code where ``-e`` is off.
    * turning it OFF accepts a ``set +e`` at any indent. Suppressing on weak
      evidence costs a missed finding; asserting on weak evidence costs a
      false one, and a linter that cries wolf is a linter somebody deletes.

    Workflow blocks get ``default=True`` from the interpreter, and their
    bodies are DEDENTED before they reach here — without that every line
    carries the YAML block indent, nothing is ever top-level, and a ``set -e``
    could never re-arm the check after a ``set +e`` in the same block.
    """
    active = default
    for raw in lines[:upto]:
        line = raw.strip()
        if not _SET_E_RE.match(line):
            continue
        top_level = raw[:1] not in (" ", "\t")
        words = line.split()[1:]
        for i, word in enumerate(words):
            if not word.startswith(("-", "+")):
                continue
            sign = word[0]
            flags = word[1:]
            if flags.startswith("o"):
                # `set -o errexit` / `set +o errexit` — the option NAME is the
                # next word, and the letters here carry no `e`. Missing this
                # made `set +o errexit` invisible, so the check stayed armed
                # over a block that had deliberately disarmed it.
                if words[i + 1 : i + 2] == ["errexit"]:
                    if sign == "+":
                        active = False
                    elif top_level:
                        active = True
                continue
            if "e" not in flags:
                continue
            if sign == "+":
                active = False
            elif top_level:
                active = True
    return active


def scan_block(
    body: str, path: str, base_line: int, e_by_default: bool
) -> list[Finding]:
    """Report every unsafe ``$?`` capture in one shell block."""
    findings: list[Finding] = []
    lines = body.split("\n")

    for idx, line in enumerate(lines):
        is_capture = bool(_CAPTURE_RE.match(line))
        is_direct = bool(_DIRECT_TEST_RE.match(line))
        if not (is_capture or is_direct):
            continue

        prev_line = lines[idx - 1] if idx else ""
        if _ALLOW_MARKER in line or _ALLOW_MARKER in prev_line:
            continue
        if not _set_e_active(lines, idx, e_by_default):
            continue

        # Walk back to the command that actually ran, skipping blanks and
        # comments, then reassemble it if it was `\`-continued.
        #
        # Both ends matter and they are different lines: the STRUCTURE that
        # makes a capture safe (`else`, `fi`) is at the start, while `|| true`
        # is at the end. Testing only the first physical line reported
        # `mycmd \ / --flag \ / || true` as a violation — a common CI shape —
        # and printed the wrong line as the context.
        end = idx - 1
        while end >= 0 and (not lines[end].strip() or lines[end].strip().startswith("#")):
            end -= 1
        start = end
        while start > 0 and lines[start - 1].rstrip().endswith("\\"):
            start -= 1
        logical = " ".join(ln.strip().rstrip("\\").strip() for ln in lines[start : end + 1])

        if _SAFE_PRECEDING.search(logical.rstrip()):
            continue
        preceding = logical

        findings.append(
            Finding(path, base_line + idx, line.strip(), preceding.strip())
        )
    return findings


def _iter_run_blocks(text: str) -> Iterable[tuple[str, int, bool]]:
    """Yield (body, 1-based start line, `-e` default) for each ``run:`` block.

    Parsed from the raw text rather than through a YAML loader on purpose:
    the loader discards line numbers and the surrounding ``shell:`` key, and a
    finding a human cannot locate is a finding nobody fixes.
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)-?\s*run:\s*\|.*$", lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        body: list[str] = []
        start = i + 2  # 1-based line number of the first body line
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                break
            body.append(nxt)
            j += 1

        # DEDENT. Every body line carries the block scalar's YAML indent, so
        # without this nothing in a `run:` block is ever "unindented" and the
        # top-level rule in _set_e_active can never re-arm -e: `set +e … set -e
        # … cmd; rc=$?` passed silently, which is a false NEGATIVE in the one
        # place the check is supposed to be unconditional.
        body_text = textwrap.dedent("\n".join(body))

        # A custom `shell:` can remove -e — but only the one on THIS step.
        # Scanning a fixed window reached into the previous step, so a
        # `shell: python` step disarmed the check for the bash step after it.
        # Walk back to the nearest step boundary instead.
        shell_spec = None
        for k in range(i - 1, -1, -1):
            stripped = lines[k].strip()
            if re.match(r"^-\s*(name|uses|run|id|with|shell)\s*:", stripped) and k != i:
                sm = re.match(r"^-\s*shell\s*:\s*(.+)$", stripped)
                if sm:
                    shell_spec = sm.group(1)
                break  # start of this step — stop, never cross into the last
            sm = re.match(r"^shell\s*:\s*(.+)$", stripped)
            if sm:
                shell_spec = sm.group(1)
                break
        e_default = True
        if shell_spec:
            spec = shell_spec.strip().strip("\"'")
            if spec in ("bash", "sh"):
                e_default = True  # Actions adds -e for these
            elif "{0}" in spec:  # fully custom command line
                # Short-option bundles only. A regex like `-\w*e` also
                # matches `--noprofile`, which would arm the check on a
                # shell that has no -e at all.
                e_default = any(
                    w.startswith("-") and not w.startswith("--") and "e" in w[1:]
                    for w in spec.split()
                )
            else:  # python, pwsh, ...
                e_default = False
        yield body_text, start, e_default
        i = j


def _rel(path: pathlib.Path) -> str:
    """Repo-relative where possible; absolute otherwise (tests scan tmp dirs)."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def scan_workflows(workflow_dir: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        rel = _rel(path)
        for body, start, e_default in _iter_run_blocks(text):
            findings.extend(scan_block(body, rel, start, e_default))
    return findings


def _is_shell_file(path: pathlib.Path) -> bool:
    if path.suffix == ".sh":
        return True
    try:
        with path.open("rb") as fh:
            return bool(_SHEBANG_RE.match(fh.readline()))
    except OSError:
        return False


def scan_scripts(dirs: Iterable[pathlib.Path]) -> tuple[list[Finding], int]:
    """Scan every shell file under ``dirs``. Returns (findings, files seen).

    The count is returned rather than discarded so ``main`` can refuse to
    print a pass over an empty scan — the failure mode this guard exists to
    catch, applied to itself.
    """
    findings: list[Finding] = []
    seen = 0
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or not _is_shell_file(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            seen += 1
            # A standalone script has -e only if it asks for it, so the
            # default is False here and a TOP-LEVEL `set -e` turns it on.
            findings.extend(scan_block(text, _rel(path), 1, False))
    return findings, seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument(
        "--workflow-dir", type=pathlib.Path, default=WORKFLOW_DIR,
        help="directory of workflow YAML to scan",
    )
    parser.add_argument(
        "--script-dir", type=pathlib.Path, action="append", default=None,
        help="also scan .sh files here (repeatable)",
    )
    args = parser.parse_args(argv)

    if not args.workflow_dir.is_dir():
        print(f"{args.workflow_dir} is not a directory", file=sys.stderr)
        return 1

    workflows = sorted(args.workflow_dir.glob("*.yml")) + sorted(
        args.workflow_dir.glob("*.yaml")
    )
    if not workflows:
        # Refusing an empty scan is the point of the whole exercise: "0
        # workflows checked" must never print the same line as "all clean".
        print(f"no workflows found in {args.workflow_dir} — refusing to report a pass",
              file=sys.stderr)
        return 1

    script_dirs = args.script_dir or list(SCRIPT_DIRS)
    missing = [d for d in script_dirs if not pathlib.Path(d).is_dir()]
    if missing and args.script_dir:
        # Only for explicitly requested dirs: a typo'd --script-dir silently
        # scanned nothing and still printed the pass line.
        print(f"--script-dir does not exist: {', '.join(str(d) for d in missing)}",
              file=sys.stderr)
        return 1

    findings = scan_workflows(args.workflow_dir)
    script_findings, scripts_seen = scan_scripts(script_dirs)
    findings += script_findings

    if not args.script_dir and scripts_seen == 0:
        print("no shell scripts found in the default script dirs — "
              "refusing to report a pass", file=sys.stderr)
        return 1

    if findings:
        print(
            f"{len(findings)} shell status capture(s) that are dead code under "
            f"`set -e`:\n", file=sys.stderr,
        )
        for f in findings:
            print(f"  ✗ {f.path}:{f.line_no}", file=sys.stderr)
            print(f"      after: {f.context}", file=sys.stderr)
            print(f"      then:  {f.line}", file=sys.stderr)
        print(
            "\n`set -uo pipefail` does NOT clear the `-e` that GitHub Actions "
            "supplies, so the\ncapture above is never reached when the command "
            "fails — which is the case it\nexists for. Use the `if` form:\n"
            "\n    if cmd; then rc=0; else rc=$?; fi\n"
            "\nor `cmd || true` if the status is not needed, or `set +e` for a "
            "long run of\ncommands. A real exception can carry "
            f"`# {_ALLOW_MARKER}`.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK — {len(workflows)} workflow(s) + {scripts_seen} shell script(s) "
        f"scanned, no unguarded `$?` captures."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
