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


def _extract(pattern: str, what: str) -> str:
    """Lift a block verbatim out of the shipped entrypoint."""
    m = re.search(pattern, ENTRYPOINT.read_text(encoding="utf-8"), re.S | re.M)
    assert m, f"entrypoint no longer contains {what}"
    return m.group(0)


def _loop() -> str:
    return _extract(r"^EXIT_CODE=0\nwhile :; do\n.*?^done$", "the child-exit poll loop")


def _term() -> str:
    """The REAL _term, not a stub.

    Stubbing it is what let the first version of these tests pass while the
    shipped path exited 139: `_term` expanded an unset RADVD_PID to 0, so
    `kill -TERM 0` signalled the whole process group, re-entered the TERM trap
    and recursed until the shell died of stack exhaustion. A stub cannot see
    that, and the exit-status assertions below were meaningless without it.
    """
    return _extract(r"^_term\(\) \{.*?^\}$", "the _term handler")


def _run(shell: str, agent_exit: int, radvd: bool = False) -> tuple[int, float, str]:
    """Run the shipped loop with stub children; return (rc, seconds, output).

    ``radvd=False`` leaves RADVD_PID EMPTY, which is the shipped default
    (radvd starts only when RADVD_MANAGED=1) and the case that reproduces the
    `kill -TERM 0` recursion. ``radvd=True`` spawns a real stand-in child so
    the managed path is exercised with a pid that is safe to signal.
    """
    radvd_setup = (
        "( while :; do sleep 300; done ) </dev/null >/dev/null 2>&1 & RADVD_PID=$!"
        if radvd
        else "RADVD_PID="
    )
    # The stubs get </dev/null >/dev/null: a `sleep` grandchild that outlives
    # its killed subshell would otherwise keep the captured stdout pipe open,
    # and subprocess.run would block on communicate() long after the shell had
    # exited — a harness timeout that reads exactly like the bug.
    # `trap _term TERM INT` and an EMPTY RADVD_PID reproduce the shipped
    # default (radvd starts only when RADVD_MANAGED=1; the image default is 0).
    # Both are load-bearing: without the trap, `kill -TERM 0` would not recurse
    # and the 139 this test exists to catch would not reproduce.
    script = f"""
{_term()}
trap _term TERM INT
( while :; do sleep 300; done ) </dev/null >/dev/null 2>&1 & KEA_PID=$!
( while :; do sleep 300; done ) </dev/null >/dev/null 2>&1 & KEA6_PID=$!
{radvd_setup}
( sleep 1; exit {agent_exit} ) </dev/null >/dev/null 2>&1 & AGENT_PID=$!
{_loop()}
_term
wait
exit "$EXIT_CODE"
"""
    argv = [shell, "-c", script] if shell != "busybox" else ["busybox", "sh", "-c", script]
    start = time.monotonic()
    # start_new_session is load-bearing, not hygiene. The bug being guarded is
    # `kill -TERM 0`, which signals the CALLER'S PROCESS GROUP — and without a
    # new session that group is pytest's own. Against the unfixed entrypoint the
    # test run was itself Terminated, so the guard both destroyed the runner and
    # reported nothing. Its own group keeps the blast radius inside the subject.
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=30, start_new_session=True
    )
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


@pytest.mark.skipif(not NO_WAIT_N, reason="no shell without working `wait -n` available")
@pytest.mark.parametrize("shell", NO_WAIT_N)
def test_term_does_not_signal_its_own_process_group(shell: str) -> None:
    """`kill -TERM 0` would take out the shell, recursively (#1043 review).

    `_term` expanded an unset RADVD_PID to `0`, and `kill -TERM 0` signals the
    CALLER'S PROCESS GROUP. With `trap _term TERM INT` that re-enters `_term`,
    recursing until the shell dies of stack exhaustion: measured, the container
    exited 139 (SIGSEGV) after ~4000 calls instead of the child's status, and
    kea was never shut down. Latent before this PR because nothing reached
    `_term` on a crash; the poll loop is what made it reachable.

    radvd unmanaged — the image default, and the case that reproduces it.
    """
    rc, _secs, out = _run(shell, agent_exit=7, radvd=False)
    assert rc != 139, f"_term killed its own process group (SIGSEGV)\n{out}"
    assert rc == 7, f"expected the agent's status 7, got {rc}\n{out}"


@pytest.mark.skipif(not NO_WAIT_N, reason="no shell without working `wait -n` available")
@pytest.mark.parametrize("shell", NO_WAIT_N)
def test_a_managed_radvd_is_still_signalled(shell: str) -> None:
    """The radvd-managed path must keep working — don't fix one by losing the other."""
    rc, _secs, out = _run(shell, agent_exit=7, radvd=True)
    assert rc == 7, f"expected 7 with a populated RADVD_PID, got {rc}\n{out}"
