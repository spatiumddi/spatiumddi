"""Process look-up shared by the DNS daemon drivers (issue #704).

Both drivers track their daemon with a per-instance ``daemon_pid`` and
call ``start_daemon()`` from two places — ``supervisor.py`` at boot and
the config-apply path — with only ``daemon_running()`` between them and
a duplicate spawn.

**The exact trigger is not fully established.** An earlier version of
this docstring blamed a second driver object; that is wrong —
``supervisor.run()`` builds exactly one driver and hands that same
object to the sync loop. What was observed on a real agent is two
``named_started`` events 118 ms apart from one process, which most
plausibly means the boot-path daemon had not yet been reflected in
``daemon_pid`` (or had already exited) when the first config apply ran
its check. Whatever the precise race, the fix is the same and does not
depend on knowing it: consult the system rather than instance state
before spawning.

The two failure modes look nothing alike, which is why this went
unnoticed for so long:

* **BIND9** uses ``SO_REUSEPORT``, so the duplicate binds :53 and :953
  happily alongside the original. Nothing errors; ``rndc`` just becomes
  a coin flip between two instances, and per-process state like query
  logging appears to ignore commands.
* **PowerDNS** does not set ``reuseport``, so the duplicate fails to
  bind and exits — but the driver keeps only ``Popen.pid`` and never
  ``wait()``s, so the corpse becomes a **zombie**. ``daemon_pid`` is
  left pointing at it, and because ``os.kill(zombie, 0)`` succeeds the
  driver believes its daemon is healthy while tracking a dead process.

Hence :func:`find_running_daemon`, which both drivers consult before
spawning — and which skips zombies, since adopting one would recreate
exactly the PowerDNS failure it exists to prevent.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from collections.abc import Iterator


def is_zombie(pid: str) -> bool:
    """True when ``pid`` is a reaped-but-not-collected corpse.

    Field 3 of ``/proc/<pid>/stat`` is the state character. The comm
    field ahead of it can contain spaces and parentheses, so the split
    is anchored on the last ``)`` rather than on whitespace.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return False
    tail = raw.rpartition(")")[2].split()
    return bool(tail) and tail[0] == "Z"


def find_running_daemon(comm: str) -> int | None:
    """PID of a live process named ``comm``, or ``None``.

    Reads ``/proc`` directly rather than shelling to ``pgrep``: the
    agent images are Alpine-based, busybox ``pgrep`` differs from
    procps, and this runs on the daemon startup path where a missing
    binary must not be able to wedge the launch.

    Zombies are skipped — a dead daemon must not be adopted as a live
    one.
    """
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        # Not a Linux host (dev machine, some CI sandboxes). Callers
        # fall back to spawning, which is the pre-existing behaviour.
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                if fh.read().strip() != comm:
                    continue
        except OSError:
            # Exited between listdir and open.
            continue
        if is_zombie(pid):
            continue
        try:
            return int(pid)
        except ValueError:
            continue
    return None


@contextlib.contextmanager
def spawn_guard(state_dir, name: str) -> Iterator[None]:
    """Serialise check-then-spawn across every caller in this container.

    ``find_running_daemon`` alone does not close the race it was written for,
    and the reason is that it matches on ``/proc/<pid>/comm``: between
    ``Popen(["named", ...])`` returning and the child completing ``execve``,
    the new process is still named after the FORKING program, so a concurrent
    caller looking for ``named`` sees nothing and spawns a second one. That is
    precisely the 118 ms window this module's docstring describes as "not
    fully established", and it still fires — observed live on a QA appliance
    2026-08-06 with two ``named`` processes (pids 14 and 24), same parent,
    same config, both holding :53 and :953 under SO_REUSEPORT, which made
    every subsequent ``rndc`` a coin flip between one daemon holding the
    current zones and one serving stale ones.

    An exclusive flock held across check AND spawn removes the window
    regardless of which caller wins. It is released on exit, so a crash
    mid-spawn cannot wedge the next start.
    """
    lock_path = os.path.join(str(state_dir), f".{name}.spawn.lock")
    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        # No writable state dir / no flock (some sandboxes). Fall back to the
        # unguarded path, which is the pre-existing behaviour.
        if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSYS):
            raise
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


def wait_for_daemon(comm: str, pid: int, timeout_s: float = 5.0) -> None:
    """Block until ``pid`` is visible under ``comm``, or the timeout expires.

    Held inside :func:`spawn_guard`, this is what makes the NEXT caller's
    ``find_running_daemon`` see the daemon we just started: it does not return
    while the child is still pre-``execve``.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                if fh.read().strip() == comm:
                    return
        except OSError:
            return  # exited, or not Linux — nothing to wait for
        time.sleep(0.02)
