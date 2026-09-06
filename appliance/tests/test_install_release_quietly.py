"""`_release_quietly` must not abort the install (#1003 item 7 regression).

Item 7 replaced

    swapoff "/dev/$part" >> "$INSTALL_LOG" 2>&1 || true

with a helper that classifies the output, and dropped the ``|| true`` on the
way. ``do_install`` runs under ``set -e`` (it turns it on itself), and the
exit status of ``out=$(cmd)`` IS cmd's — so the FIRST swapoff of a partition
that is not swap, which is the normal case and the entire reason the helper
exists, aborted the install:

    _release_quietly() local out rc
    swapoff /dev/sda1
    out='swapoff: /dev/sda1: swapoff failed: Invalid argument'
    do_install() on_failure

A cosmetic log fix turned into a total install failure, on every machine.

These tests EXECUTE the function under ``set -e``. Nothing structural would
have caught it: the code reads perfectly, and the defect lives entirely in
the interaction between a command substitution's exit status and a shell
option set 80 lines away in a different function. That combination is also
why the rest of the suite missed it — no test drives ``do_install``.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from _installer_source import SRC, extract_fn

_FN = extract_fn("_release_quietly")


def _run(cmd: str, *, set_e: bool = True) -> subprocess.CompletedProcess[str]:
    """Run _release_quietly under do_install's own shell options."""
    opts = "set -euo pipefail" if set_e else "set -uo pipefail"
    script = f"""
        {opts}
        TRACE_LOG=$(mktemp)
        INSTALL_LOG=$(mktemp)
        {_FN}
        {cmd}
        echo "SURVIVED"
        echo "TRACE:$(cat "$TRACE_LOG")"
        echo "INSTALL:$(cat "$INSTALL_LOG")"
    """
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def test_do_install_really_does_set_e() -> None:
    """The premise. If this stops being true the rest proves nothing."""
    body = SRC[SRC.index("\ndo_install() {") :]
    body = body[: body.index("\n}\n")]
    assert re.search(r"^\s*set -e\s*$", body, re.MULTILINE), (
        "do_install no longer enables set -e — re-derive what these tests guard"
    )


def test_a_failing_command_does_not_abort() -> None:
    """THE REGRESSION: swapoff on a non-swap partition is the normal case."""
    r = _run("_release_quietly false")
    assert "SURVIVED" in r.stdout, (
        f"the install aborted on an expected failure (rc={r.returncode}): {r.stderr}"
    )


def test_expected_noise_goes_to_the_trace_log() -> None:
    r = _run("""_release_quietly sh -c 'echo "umount: /dev/sda1: not mounted." >&2; exit 1'""")
    assert "SURVIVED" in r.stdout
    trace = next(l for l in r.stdout.splitlines() if l.startswith("TRACE:"))
    install = next(l for l in r.stdout.splitlines() if l.startswith("INSTALL:"))
    assert "not mounted" in trace
    assert "not mounted" not in install, "expected noise must stay out of the install log"


def test_a_real_surprise_goes_to_the_install_log() -> None:
    """The filter must not swallow a failure nobody predicted."""
    r = _run("""_release_quietly sh -c 'echo "umount: target is busy" >&2; exit 1'""")
    assert "SURVIVED" in r.stdout
    install = next(l for l in r.stdout.splitlines() if l.startswith("INSTALL:"))
    assert "target is busy" in install


def test_success_is_recorded_and_returns_zero() -> None:
    r = _run("_release_quietly true")
    assert "SURVIVED" in r.stdout
    trace = next(l for l in r.stdout.splitlines() if l.startswith("TRACE:"))
    assert "released" in trace


@pytest.mark.parametrize("phrase", ["not mounted", "Invalid argument", "not found"])
def test_the_real_messages_are_classified_as_noise(phrase: str) -> None:
    """The strings the tools actually emit, from the #1003 install log."""
    r = _run(f"""_release_quietly sh -c 'echo "swapoff: /dev/sda1: {phrase}" >&2; exit 255'""")
    assert "SURVIVED" in r.stdout
    install = next(l for l in r.stdout.splitlines() if l.startswith("INSTALL:"))
    assert install == "INSTALL:", f"{phrase!r} should not reach the install log"
