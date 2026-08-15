"""One correct way to put a secret on disk (issue #869).

Four copies of the same ``os.open(O_NOFOLLOW, 0o600)`` incantation had grown
across the drivers — the PowerDNS API-key store, the DoT/DoH listener key,
Technitium's token store, and (briefly) the rendered-config writer. They
agreed on the mode and disagreed on everything else: only three did the
tmp+``replace`` dance, and none of them checked how many bytes ``os.write``
actually wrote.

Both properties are load-bearing:

* **Mode at creation.** ``write_text`` + ``chmod`` leaves a window where the
  file exists at the umask default (0644) with the secret already in it; a
  crash in that window leaves it there permanently. ``O_NOFOLLOW`` additionally
  refuses to follow a symlink planted at the destination.
* **Atomic replace.** A reader (``pdns_server``, the dnsdist front, the agent
  itself after a restart) must never observe a half-written secret file. The
  content lands on a sibling tmp path and is renamed into place, which is
  atomic within a filesystem.
* **Complete write.** ``os.write`` is not obliged to write everything it is
  given; on ENOSPC/EDQUOT it returns a short count rather than raising. The
  loop below turns a partial write into an ``OSError``, because the callers'
  error handling assumes a failed write raises — a silently truncated config
  is worse than no config, since downstream code parses it and gives up.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

__all__ = ["write_private", "harden_mode"]

_SECRET_MODE = 0o600


def _write_all(fd: int, payload: bytes) -> None:
    """``os.write`` until the buffer is drained, or raise.

    A short write means the filesystem could not take the rest (out of space
    or over quota). Raising here is deliberate: every caller's contract is
    "this raises on failure", and the sync loops upstream rely on the
    exception propagating so they do not advance their etag past a config
    that was never fully written.
    """
    written = 0
    while written < len(payload):
        n = os.write(fd, payload[written:])
        if n <= 0:
            raise OSError(
                f"short write: {written} of {len(payload)} bytes "
                "(filesystem full or over quota?)"
            )
        written += n


def write_private(path: Path, text: str, *, atomic: bool = True) -> None:
    """Write ``text`` to ``path`` as 0600, atomically, or raise.

    ``atomic=False`` writes in place — only for a destination inside a
    freshly-created staging directory that no reader can see yet, where the
    directory rename is already the atomic step.
    """
    payload = text.encode()
    target = path.with_suffix(path.suffix + ".new") if atomic else path
    fd = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        _SECRET_MODE,
    )
    try:
        _write_all(fd, payload)
    finally:
        os.close(fd)
    if atomic:
        target.replace(path)


def harden_mode(path: Path) -> None:
    """Force 0600 on a file that already exists, if it exists.

    Remediation for files written by an OLDER agent build before the secret
    ever went through :func:`write_private`. An upgrade alone does not fix
    what is already on disk: the previous render survives as ``rendered/``
    until the next structural change replaces it, and then as
    ``rendered.prev/`` after that — so a still-live credential can sit at
    0644 across two renders' worth of uptime unless something reaches back
    and fixes it.

    Best-effort by construction: this runs at the top of a render, and
    failing to re-mode a file from a previous build must never be the thing
    that stops the agent applying config. Both swallowed cases are logged
    rather than silently dropped.
    """
    try:
        path.chmod(_SECRET_MODE)
    except FileNotFoundError:
        # Nothing to harden. The common case — a clean install has no
        # previous render, and callers pass every candidate path
        # unconditionally rather than pre-checking each one.
        log.debug("harden_mode_absent", path=str(path))
    except OSError as exc:
        # Typically PermissionError: the file exists but belongs to another
        # uid (the container entrypoint writes some state as root before
        # chown'ing it). We cannot fix that from here, and raising would
        # turn "couldn't tighten an old file" into "the agent stopped
        # rendering config" — strictly worse than the exposure it guards.
        # Surfaced at WARNING so the operator can fix ownership.
        log.warning("harden_mode_failed", path=str(path), error=str(exc))
