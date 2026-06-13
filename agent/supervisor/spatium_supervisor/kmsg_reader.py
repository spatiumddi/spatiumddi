"""Realtime nftables-drop log reader for the Firewall → Logs viewer (#404).

nft `log` statements land in the kernel ring buffer; `/dev/kmsg` exposes it.
This module runs a daemon thread that tails `/dev/kmsg`, keeps only the lines
our firewall renderer prefixes with ``spatium-fw: `` (#404 — a rate-limited
catch-all log before the chain's policy drop), and buffers them with their
kernel sequence number so the ``firewall_logs`` nettool can serve incremental
batches (since-cursor) to the control plane.

The supervisor pod runs privileged + as root, so `/dev/kmsg` (host kernel log)
is readable. We seek to the end on open so we only surface NEW drops — i.e. the
operator flips logging on, reproduces the blocked connection, and watches it
appear — rather than replaying the whole boot-time kernel buffer.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from collections import deque

import structlog

log = structlog.get_logger(__name__)

# Only buffer lines our renderer tags — keeps boot/kernel noise out and the
# buffer cheap. Matches the prefix in backend firewall._FIREWALL_LOG_RULE.
_PREFIX = "spatium-fw:"
_KMSG = "/dev/kmsg"
_MAX = 1000

_buf: deque[tuple[int, int, str]] = deque(maxlen=_MAX)  # (seq, ts_us, text)
_lock = threading.Lock()
# Reader state as threading primitives rather than rebound module globals:
# ``_ready`` is set once /dev/kmsg is open (cleared if the reader exits), and
# ``_spawned`` guards the idempotent single-thread start.
_ready = threading.Event()
_spawn_lock = threading.Lock()
_spawned = threading.Event()


def is_available() -> bool:
    """True once the reader has opened /dev/kmsg successfully."""
    return _ready.is_set()


def get_since(since_seq: int, limit: int) -> tuple[list[tuple[int, int, str]], int]:
    """Return buffered entries with ``seq > since_seq`` (oldest-first, capped at
    ``limit``) plus the newest seq seen (the next since-cursor)."""
    with _lock:
        snapshot = list(_buf)
    fresh = [e for e in snapshot if e[0] > since_seq][:limit]
    cursor = snapshot[-1][0] if snapshot else since_seq
    return fresh, cursor


def _parse(raw: str) -> tuple[int, int, str] | None:
    """Parse a /dev/kmsg record ``prio,seq,ts_us,flags;message`` → (seq, ts, msg).

    Continuation/dictionary lines (``\\n KEY=val``) are dropped to the first
    line; a malformed header yields None.
    """
    header, sep, message = raw.partition(";")
    if not sep:
        return None
    parts = header.split(",")
    if len(parts) < 3:
        return None
    try:
        seq = int(parts[1])
        ts_us = int(parts[2])
    except ValueError:
        return None
    return seq, ts_us, message.split("\n", 1)[0].strip()


def _loop() -> None:
    # Open /dev/kmsg, retrying — a privileged container can briefly see EPERM
    # on /dev/kmsg right at pod start (before the device settles), and we must
    # not let that transient failure kill the reader for the life of the pod.
    fd = -1
    for attempt in range(60):  # ~5 min of retries, then give up
        try:
            fd = os.open(_KMSG, os.O_RDONLY | os.O_NONBLOCK)
            break
        except OSError as exc:
            if attempt == 0:
                log.warning("supervisor.kmsg.open_retry", error=str(exc))
            time.sleep(5.0)
    if fd < 0:
        log.warning("supervisor.kmsg.open_gave_up")
        return
    # Skip to the newest record — surface only drops logged from now on.
    try:
        os.lseek(fd, 0, os.SEEK_END)
    except OSError:
        # Non-fatal: if the seek fails we just read from the current position
        # (some backlog may show), so there's nothing to handle — keep going.
        pass
    _ready.set()
    log.info("supervisor.kmsg.reader_started")
    try:
        while True:
            try:
                data = os.read(fd, 8192)
            except BlockingIOError:
                time.sleep(0.5)
                continue
            except OSError as exc:
                # EPIPE: records were overwritten between reads — the fd auto-
                # advances to the next available record, so just keep going.
                if exc.errno == errno.EPIPE:
                    continue
                log.warning("supervisor.kmsg.read_failed", error=str(exc))
                time.sleep(1.0)
                continue
            if not data:
                time.sleep(0.5)
                continue
            raw = data.decode("utf-8", "replace").rstrip("\n")
            if _PREFIX not in raw:
                continue
            parsed = _parse(raw)
            if parsed is not None:
                with _lock:
                    _buf.append(parsed)
    finally:
        os.close(fd)
        _ready.clear()


def start() -> None:
    """Spawn the kmsg reader daemon thread (idempotent + race-safe)."""
    with _spawn_lock:
        if _spawned.is_set():
            return
        _spawned.set()
    threading.Thread(target=_loop, name="spatium-fw-kmsg", daemon=True).start()
