"""The bake's staleness guard must actually run on the build host (#991 §4).

``bake-images.sh`` refuses to bake a SpatiumDDI source image older than
24 h, so an operator who edits code and forgets ``make build`` does not
silently ship a stale ISO.  It parsed Docker's RFC 3339 timestamp with
``date -d``, which is **GNU-only**.  On macOS — where the arm64
cross-build work put this script — that parse fails, ``image_age_seconds``
returns non-zero, and the caller's ``|| continue`` skips the check for
every image.  The guard did not fire late or wrongly: it did not exist,
and said nothing about it.

That is the failure mode worth a test.  A guard whose default on error is
"skip" prints a clean run of passes whether or not it works, so these
tests assert on the *parsed value* rather than on an exit code, and the
"guard is inert" path has to announce itself.

The tests execute the real ``rfc3339_to_epoch`` extracted from the shipped
script — the bytes that ship, not a transcription — under both date
dialects available on the host.

HOW TO RUN (from the repo root or this directory):
    python3 -m pytest appliance/tests/test_bake_stale_guard_portable.py -v

No Docker, no root, no appliance ISO required.
"""

from __future__ import annotations

import subprocess

import pytest

from _installer_source import SCRIPTS, extract_fn  # noqa: E402 - sibling module

SCRIPT = SCRIPTS / "bake-images.sh"


def _extract(func: str) -> str:
    """One shell function out of the shipped script, verbatim.

    Uses the suite's shared extractor rather than a sixth respelling —
    ``_installer_source`` was added precisely because five had
    accumulated, and its docstring asks new files to import from there.
    """
    return extract_fn(func, SCRIPT.read_text())


def _run(snippet: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def to_epoch() -> str:
    return _extract("rfc3339_to_epoch")


def test_the_shipped_helper_parses_a_real_docker_timestamp(to_epoch):
    """Docker emits nanosecond precision and a ``Z`` suffix.

    2026-09-07T19:29:21.886604167Z is a real ``docker image inspect
    --format '{{.Created}}'`` value; 1788809361 is that instant in epoch
    seconds. Pinning the number rather than just "it succeeded" is what
    makes this a test of the parse rather than of the exit code.
    """
    out = _run(to_epoch + "\nrfc3339_to_epoch '2026-09-07T19:29:21.886604167Z'")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1788809361"


def test_it_parses_a_timestamp_with_no_fractional_part(to_epoch):
    out = _run(to_epoch + "\nrfc3339_to_epoch '2026-09-07T19:29:21Z'")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1788809361"


def test_it_fails_loudly_on_something_that_is_not_a_timestamp(to_epoch):
    """It must return non-zero rather than emit a bogus epoch — the
    caller distinguishes "could not read a date" from "not stale", and
    conflating them is how the guard went missing in the first place.
    """
    out = _run(to_epoch + "\nrfc3339_to_epoch 'not-a-timestamp'")
    assert out.returncode != 0
    assert out.stdout.strip() == ""


def test_both_date_dialects_are_attempted():
    """The GNU form alone is what broke on macOS; the BSD form alone
    would break on every Linux CI runner. Both must be present, and in
    an order where the GNU one is tried first (it is what CI runs).
    """
    body = _extract("rfc3339_to_epoch")
    gnu = body.index("date -u -d")
    bsd = body.index("date -u -j -f")
    assert gnu < bsd, "the GNU form should be tried first — it is what CI uses"


def test_an_unreadable_date_is_reported_rather_than_silently_skipped():
    """A guard that quietly evaluates nothing is indistinguishable from a
    guard that passed. The pre-scan must say so when it could not read a
    build date, and must NOT abort the build over it — an unreadable
    timestamp is no evidence that the image is stale.
    """
    body = SCRIPT.read_text()
    scan = body[body.index("if [ \"$BAKE_SOURCE\" = \"local\" ]") :]
    scan = scan[: scan.index("\nfi\n")]
    assert "undated+=" in scan, "images with an unreadable date must be collected"
    assert "did NOT run" in scan, "the inert case must announce itself"
    # The warning branch must not exit; only the genuinely-stale branch does.
    warn_block = scan[scan.index('if [ "${#undated[@]}" -gt 0 ]') :]
    warn_block = warn_block[: warn_block.index("\n    fi")]
    assert "exit" not in warn_block


def test_the_caller_still_distinguishes_stale_from_unreadable():
    """Regression guard for the original bug's shape: the old code used
    ``age="$(image_age_seconds ...)" || continue``, which collapsed a
    parse failure into "fine". The replacement must branch on the
    function's status explicitly.
    """
    body = SCRIPT.read_text()
    assert 'age="$(image_age_seconds "$src")" || continue' not in body
    assert 'if ! age="$(image_age_seconds "$src")"; then' in body
