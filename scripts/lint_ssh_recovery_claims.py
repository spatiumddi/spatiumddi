#!/usr/bin/env python3
"""No surface may promise SSH as an unconditional recovery path (#1009).

Before #1009 the appliance opened ``tcp dport 22`` unconditionally and could
not stop doing so, and a dozen places said as much — in code comments, in
docs, and in two 422 details and four pieces of UI copy an operator reads at
the moment they are deciding whether to restrict something.

``ssh_lockdown`` made that false in one specific case: it retires the port-22
floor, so an operator who turns it on with a scope that excludes them, AND has
also scoped the Web UI, is left with the console. #1013 added a cross-setting
guard that refuses that combination without an explicit acknowledgement, so the
operator is no longer only warned by prose — but the prose still has to be
true. A surface that promises SSH unconditionally contradicts the refusal they
are about to meet, which is worse than saying nothing.

Correcting those surfaces took four passes, and each one grepped for the
PREVIOUS phrasing and so missed the next paraphrase:

  1. ``un-removable``          — corrected ten places (#1009)
  2. Copilot on the PR         — two more the first pass walked past
  3. ``stays open regardless`` — three more, two of them user-facing
  4. Copilot again             — a seventh, which THIS linter's first cut
                                 also missed (its sentence never said "ssh",
                                 and its neighbour said "sshd")

So this asserts on the CLAIM rather than on any wording: port 22 stated with
an absolute, whose surrounding sentences never name the exception.

WHY A LINTER AND NOT A TEST. It began as ``backend/tests/`` and did not
belong there. It reads ``frontend/src`` — where four of the seven claims
lived — and the backend suite is path-filtered off frontend changes, so a
frontend-only PR would never have run it. Declaring the read instead would
mean carving ``frontend/src/`` into ``ci-backend-must-run.txt``, i.e. running
eight backend shards for a CSS edit. Backend Lint is not path-filtered and
needs no database, which is exactly this check's shape.

Exit non-zero (with a human-readable report) on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Files an operator's eyes or an operator's API client actually reach.
#: Design docs are excluded — ``docs/design/FLEET_FIREWALL.md`` records the
#: historical design and carries its own annotation about where it was wrong.
SEARCH: tuple[tuple[str, str], ...] = (
    ("backend/app", "*.py"),
    ("frontend/src", "*.tsx"),
    ("frontend/src", "*.ts"),
    ("docs/deployment", "*.md"),
    ("docs/features", "*.md"),
    ("appliance/mkosi.extra", "*"),
)

#: A sentence asserting something unconditional…
ABSOLUTE = re.compile(r"\b(always|never|regardless|unconditional\w*)\b", re.I)
#: …about SSH. ``sshd`` counts: prose about the daemon and prose about the
#: protocol make the same promise, and much of the copy carrying these claims
#: says ``sshd``.
SSH = re.compile(r"\bsshd?\b", re.I)
PORT22 = re.compile(r"(?<![\d.])22\b|port[- ]22|dport 22|:22\b", re.I)
#: ...but "a NON-22 ssh port" asserts nothing about 22. Stripped before the
#: port test rather than excluded by a lookbehind, because the hyphen in
#: ``non-22`` is the same character as the one in the legitimate ``port-22``.
NOT_22 = re.compile(r"\bnon-?22\b", re.I)
#: …that does NOT name the exception.
EXCEPTION = re.compile(r"lockdown|ssh_lockdown|unless|except|console|retire", re.I)

#: How far past the matched sentence to look for SSH and for the exception.
#:
#: Sentence-level scanning alone is wrong in both directions. Too strict: a
#: comment that opens "port 22 is opened unconditionally by a management
#: floor" and names the retirement in its NEXT sentence is correct, and a
#: reader meets both. Too lax: the seventh claim's own sentence never
#: mentioned SSH at all, so a sentence-scoped SSH test skipped it.
CONTEXT_SENTENCES = 2


def sentences(text: str) -> list[str]:
    """Split on sentence ends, after flattening wrapped lines.

    Comments and JSX wrap mid-sentence, so a line-based scan would see
    "SSH on port 22 is never restricted, so a" and miss whether the exception
    is named two lines later.
    """
    return re.split(r"(?<=[.!?]) ", re.sub(r"\s+", " ", text))


def offenders() -> list[str]:
    out: list[str] = []
    for subdir, glob in SEARCH:
        root = REPO_ROOT / subdir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(glob)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.resolve() == Path(__file__).resolve():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "ssh" not in text.lower():
                continue
            parts = sentences(text)
            for i, sentence in enumerate(parts):
                # The CLAIM must be in one sentence: port 22, stated
                # unconditionally. That keeps the check specific.
                if not PORT22.search(NOT_22.sub("", sentence)):
                    continue
                if not ABSOLUTE.search(sentence):
                    continue
                window = " ".join(parts[max(0, i - 1) : i + 1 + CONTEXT_SENTENCES])
                # The SSH reference and the exception may both sit in a
                # neighbouring sentence, and routinely do.
                if not SSH.search(window):
                    continue
                if EXCEPTION.search(window):
                    continue
                rel = path.relative_to(REPO_ROOT)
                out.append(f"{rel}: {sentence.strip()[:160]}")
    return out


def self_check() -> list[str]:
    """A scanner whose regexes never match is a scanner that always passes.

    Run on every invocation rather than parked in a test, so the property is
    verified wherever the linter runs. The fixtures are the real phrasings
    that reached production — each written by someone who had just read the
    previous one and paraphrased it.
    """
    problems: list[str] = []

    shipped_and_wrong = [
        "SSH on port 22 stays open regardless, so the appliance is recoverable.",
        "SSH on port 22 is never restricted, so a mistake here is fine.",
        "The un-removable SSH floor on :22 is always open.",
    ]
    for s in shipped_and_wrong:
        if not (SSH.search(s) and PORT22.search(s) and ABSOLUTE.search(s)):
            problems.append(f"would no longer flag: {s}")
        if EXCEPTION.search(s):
            problems.append(f"wrongly excused: {s}")

    # The claim split across sentences — the seventh, and the reason SSH is
    # tested against the window rather than the matched sentence.
    split = (
        "Rendered config is validated host-side via sshd -t before activation. "
        "Port 22 always stays open in the host firewall as an escape hatch."
    )
    claim = sentences(split)[1]
    if SSH.search(claim):
        problems.append("fixture drift: the split claim's own sentence says SSH")
    if not (PORT22.search(claim) and ABSOLUTE.search(claim) and SSH.search(split)):
        problems.append(f"would no longer flag the split claim: {split}")

    # A sentence about a NON-22 port asserts nothing about the floor.
    non22 = (
        "The nft fragment is the only thing that opens a NON-22 ssh port, and "
        "`nft -c -f` has always refused a malformed CIDR."
    )
    if PORT22.search(NOT_22.sub("", non22)):
        problems.append("false positive: a NON-22 sentence is treated as a 22 claim")

    # ...and the corrected forms must pass, or the linter just bans the topic.
    corrected = [
        (
            "The console always recovers it; SSH does too unless the SSH "
            "source restriction is on and excludes you."
        ),
        (
            "SSH on port 22 is open by default — the floor is retired only "
            "under lockdown."
        ),
    ]
    for s in corrected:
        if not EXCEPTION.search(s):
            problems.append(f"would wrongly flag a corrected sentence: {s}")

    return problems


def main() -> int:
    broken = self_check()
    if broken:
        print("ssh recovery-claim lint SELF-CHECK FAILED:\n", file=sys.stderr)
        for b in broken:
            print(f"  - {b}", file=sys.stderr)
        print(
            "\nThe scanner can no longer detect what it exists to detect.",
            file=sys.stderr,
        )
        return 1

    found = offenders()
    if found:
        print("ssh recovery-claim lint FAILED:\n", file=sys.stderr)
        for f in found:
            print(f"  - {f}", file=sys.stderr)
        print(
            f"\n{len(found)} surface(s) assert SSH/22 is unconditionally open, "
            "which ssh_lockdown (#1009) made false.\n"
            "Name the exception (lockdown / unless / console / retire) in the "
            "sentence or one adjacent to it, or drop the absolute.",
            file=sys.stderr,
        )
        return 1

    print("ssh recovery-claim lint OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
