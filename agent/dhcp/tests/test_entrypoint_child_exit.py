"""The kea container must die when its agent does (#1043).

`agent/dhcp/images/kea/entrypoint.sh` backgrounds kea-dhcp4, kea-dhcp6 and
the Python agent, then waits for the first of them to exit so the container
restarts. It used to do that with ``wait -n "$KEA_PID" "$KEA6_PID"
"$AGENT_PID" || wait …``, and the comment claimed busybox ash "accepts it too
as of 1.30+". It accepts the FLAG and ignores the SEMANTICS:

    busybox ash 1.37   `wait -n A B` returned only when BOTH had exited
    dash               rejects `-n` outright (rc 2) -> falls through to the
                       plain `wait`, which also waits for all of them
    bash               real any-child semantics

The image's /bin/sh is busybox ash, so when the agent died the wait sat on
the two kea supervise loops — which are restart loops that never exit — and
the container ran on with Kea serving a frozen config and no agent in it,
reporting 1/1 Running with an unchanged restart count.

These tests run the SHIPPED loop, extracted from the real file, under a shell
that lacks working `wait -n` — which is the only condition under which the
bug is visible. Stub children stand in for the three real ones: two that
never exit (the kea supervisors) and one that exits (the agent).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).parents[1] / "images" / "kea" / "entrypoint.sh"

#: Shells without working `wait -n`. dash is /bin/sh on Debian/Ubuntu
#: runners; busybox ash is what the image actually runs and is verified
#: locally (it is not installed on the CI runner).
NO_WAIT_N = [s for s in ("dash", "busybox") if shutil.which(s)]


def _loop() -> str:
    """The real child-exit loop, lifted verbatim out of the entrypoint."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    m = re.search(r"^EXIT_CODE=0\nwhile :; do\n.*?^done$", src, re.S | re.M)
    assert m, "entrypoint no longer contains the child-exit poll loop"
    return m.group(0)


def _run(shell: str, agent_exit: int) -> tuple[int, float, str]:
    """Run the shipped loop with stub children; return (rc, seconds, output)."""
    # The stubs get </dev/null >/dev/null: a `sleep` grandchild that outlives
    # its killed subshell would otherwise keep the captured stdout pipe open,
    # and subprocess.run would block on communicate() long after the shell had
    # exited — a harness timeout that reads exactly like the bug.
    script = f"""
_term() {{ echo "TERM_RAN"; }}
( while :; do sleep 300; done ) </dev/null >/dev/null 2>&1 & KEA_PID=$!
( while :; do sleep 300; done ) </dev/null >/dev/null 2>&1 & KEA6_PID=$!
( sleep 1; exit {agent_exit} ) </dev/null >/dev/null 2>&1 & AGENT_PID=$!
{_loop()}
_term
kill "$KEA_PID" "$KEA6_PID" 2>/dev/null
wait
exit "$EXIT_CODE"
"""
    argv = [shell, "-c", script] if shell != "busybox" else ["busybox", "sh", "-c", script]
    start = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return proc.returncode, time.monotonic() - start, proc.stdout + proc.stderr


@pytest.mark.skipif(not NO_WAIT_N, reason="no shell without working `wait -n` available")
@pytest.mark.parametrize("shell", NO_WAIT_N)
def test_agent_death_takes_the_container_down(shell: str) -> None:
    """The whole point: the agent exits, so must the container — promptly.

    Before the fix this blocked until something killed it, because the two
    kea supervisors never exit. 10 s is generous against a 1 s poll; the
    failure mode is "never", not "slow".
    """
    rc, secs, out = _run(shell, agent_exit=2)
    assert rc == 2, f"expected the agent's own status, got {rc}\n{out}"
    assert secs < 10, f"took {secs:.1f}s — the loop is not noticing the exit\n{out}"
    assert "TERM_RAN" in out, "kea was not shut down cleanly on the way out"
    assert "spatium-dhcp-agent" in out, "the log line must name which child exited"


@pytest.mark.skipif(not NO_WAIT_N, reason="no shell without working `wait -n` available")
@pytest.mark.parametrize("shell", NO_WAIT_N)
def test_a_clean_agent_exit_is_still_reported_as_clean(shell: str) -> None:
    """Exit status is the child's, not a fixed 1 — the orchestrator reads it."""
    rc, _secs, out = _run(shell, agent_exit=0)
    assert rc == 0, f"expected 0, got {rc}\n{out}"


def test_the_broken_wait_n_idiom_is_gone() -> None:
    """Structural backstop: no executable `wait -n` may come back.

    Prose about it is fine (the loop's own comment explains the history), so
    this looks only at non-comment lines.
    """
    code = [
        ln
        for ln in ENTRYPOINT.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    offenders = [ln for ln in code if re.search(r"\bwait\s+-n\b", ln)]
    assert not offenders, f"`wait -n` is not portable to busybox ash: {offenders}"


def test_every_supervised_child_is_polled() -> None:
    """All three pids must be in the loop, or one death goes unnoticed."""
    loop = _loop()
    for pid in ("$KEA_PID", "$KEA6_PID", "$AGENT_PID"):
        assert pid in loop, f"{pid} is not watched by the child-exit loop"
