"""Non-blocking terminal key reader for the TUI (no external deps).

Puts the terminal in cbreak/raw mode on a background thread, decodes the common
escape sequences (F-keys, arrows, Enter/Esc/Tab) plus printable characters, and
pushes normalized key tokens onto a thread-safe queue the render loop drains. F-keys
are flaky across terminals, so the TUI always also accepts letter shortcuts — this
reader just surfaces whatever it can decode.
"""

from __future__ import annotations

import os
import queue
import select
import sys
import threading

# Normalized key tokens.
UP, DOWN, LEFT, RIGHT = "UP", "DOWN", "LEFT", "RIGHT"
ENTER, ESC, TAB, BACKSPACE = "ENTER", "ESC", "TAB", "BACKSPACE"
F1, F2, F3, F4, F5, F6, F7, F8 = "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"
CTRL_C = "CTRL_C"

# Escape-sequence → token map (vt100 `\x1bO*` F-keys + `\x1b[*` arrows/`~` F-keys).
_SEQ = {
    "\x1bOP": F1, "\x1bOQ": F2, "\x1bOR": F3, "\x1bOS": F4,
    "\x1b[11~": F1, "\x1b[12~": F2, "\x1b[13~": F3, "\x1b[14~": F4,
    "\x1b[15~": F5, "\x1b[17~": F6, "\x1b[18~": F7, "\x1b[19~": F8,
    "\x1b[A": UP, "\x1b[B": DOWN, "\x1b[C": RIGHT, "\x1b[D": LEFT,
    "\x1bOA": UP, "\x1bOB": DOWN, "\x1bOC": RIGHT, "\x1bOD": LEFT,
}


def is_a_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _decode(buf: str) -> list[str]:
    """Greedily decode a raw input chunk into a list of key tokens."""
    out: list[str] = []
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch == "\x1b":
            # Try the longest known escape sequence first.
            matched = None
            for seqlen in (5, 4, 3, 2):
                if i + seqlen <= n and buf[i:i + seqlen] in _SEQ:
                    matched = (_SEQ[buf[i:i + seqlen]], seqlen)
                    break
            if matched:
                out.append(matched[0])
                i += matched[1]
            else:
                out.append(ESC)
                i += 1
            continue
        if ch in ("\r", "\n"):
            out.append(ENTER)
        elif ch == "\t":
            out.append(TAB)
        elif ch in ("\x7f", "\x08"):
            out.append(BACKSPACE)
        elif ch == "\x03":
            out.append(CTRL_C)
        elif ch.isprintable():
            out.append(ch)
        i += 1
    return out


class KeyReader:
    """Background reader: tokens land on ``.queue`` (a ``queue.Queue[str]``)."""

    def __init__(self) -> None:
        self.queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd = sys.stdin.fileno()
        self._saved = None

    def __enter__(self) -> "KeyReader":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        if not is_a_tty():
            raise RuntimeError("KeyReader requires an interactive TTY")
        import termios
        import tty
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._thread = threading.Thread(target=self._loop, name="tui-keys", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([self._fd], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(self._fd, 32)
            except OSError:
                break
            if not data:
                continue
            for tok in _decode(data.decode("utf-8", errors="ignore")):
                self.queue.put(tok)

    def get_nowait(self) -> str | None:
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> list[str]:
        out: list[str] = []
        while True:
            t = self.get_nowait()
            if t is None:
                return out
            out.append(t)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._saved is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            self._saved = None
