"""Process look-up shared by the DNS daemon drivers (issue #704).

Both drivers track their daemon with a per-instance ``daemon_pid``, and
both call ``start_daemon()`` from two places (``supervisor.py`` at boot
and the config-apply path). A second driver object therefore sees
``daemon_running() is False`` and spawns a duplicate daemon.

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

import os


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
