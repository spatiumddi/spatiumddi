"""Tails Kea's ``kea-dhcp4.log`` file and ships batches to the control plane.

The agent's ``render_kea`` adds a file ``output_options`` entry to
the Kea logger config (``/var/log/kea/kea-dhcp4.log`` by default;
overridable via ``DHCP_LOG_PATH``). Kea handles rotation in-process
via its ``maxsize`` / ``maxver`` settings — we just follow the
file like ``tail -F`` and re-open on inode change.

Same shape as the DNS agent's ``QueryLogShipper`` — see that module
for the resilience design notes (file may not exist yet on first
boot, control plane unreachable → bounded ring buffer, etc).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx
import structlog

from .config import AgentConfig

log = structlog.get_logger(__name__)

DEFAULT_DHCP_LOG_PATH = "/var/log/kea/kea-dhcp4.log"

MAX_BATCH = 200
BATCH_INTERVAL = 5.0
MAX_BUFFER_LINES = 5_000
TAIL_POLL_INTERVAL = 0.5
FILE_WAIT_INTERVAL = 5.0


class LogShipper:
    """Tail thread + batching POST loop."""

    def __init__(self, cfg: AgentConfig, token_ref: list[str], path: str | None = None) -> None:
        self.cfg = cfg
        self.token_ref = token_ref
        self.path = Path(path or os.environ.get("DHCP_LOG_PATH") or DEFAULT_DHCP_LOG_PATH)
        self._stop = threading.Event()
        self._buffer: list[str] = []
        self._last_flush = time.monotonic()
        self._fh = None  # type: ignore[var-annotated]
        self._inode: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def _cp_client(self) -> httpx.Client:
        verify: bool | str = True
        if self.cfg.insecure_skip_tls_verify:
            verify = False
        elif self.cfg.tls_ca_path:
            verify = self.cfg.tls_ca_path
        return httpx.Client(base_url=self.cfg.control_plane_url, verify=verify, timeout=15.0)

    def _open(self) -> bool:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return False
        try:
            self._fh = self.path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("dhcp_log_open_failed", path=str(self.path), error=str(exc))
            return False
        self._fh.seek(0, os.SEEK_END)
        self._inode = st.st_ino
        log.info("dhcp_log_attached", path=str(self.path), inode=self._inode)
        return True

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None
        self._inode = None

    def _check_rotation(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._close()
            return
        if self._inode is not None and st.st_ino != self._inode:
            log.info("dhcp_log_rotated", path=str(self.path))
            self._close()
            self._open()

    def _read_available(self) -> None:
        if self._fh is None:
            return
        while True:
            try:
                line = self._fh.readline()
            except OSError as exc:
                log.warning("dhcp_log_read_failed", error=str(exc))
                self._close()
                return
            if not line:
                break
            if len(self._buffer) >= MAX_BUFFER_LINES:
                drop = MAX_BUFFER_LINES // 2
                self._buffer = self._buffer[drop:]
                log.warning("dhcp_log_buffer_trimmed", dropped=drop)
            self._buffer.append(line.rstrip("\n"))

    def _should_flush(self) -> bool:
        if not self._buffer:
            return False
        if len(self._buffer) >= MAX_BATCH:
            return True
        return (time.monotonic() - self._last_flush) >= BATCH_INTERVAL

    def _flush(self) -> None:
        batch = self._buffer[:MAX_BATCH]
        self._buffer = self._buffer[MAX_BATCH:]
        try:
            with self._cp_client() as c:
                resp = c.post(
                    "/api/v1/dhcp/agents/log-entries",
                    json={"lines": batch},
                    headers={"Authorization": f"Bearer {self.token_ref[0]}"},
                )
            if resp.status_code not in (200, 204):
                log.warning(
                    "dhcp_log_ship_failed",
                    status=resp.status_code,
                    batch_size=len(batch),
                )
        except httpx.HTTPError as exc:
            log.warning("dhcp_log_ship_http_error", error=str(exc), batch_size=len(batch))
        finally:
            self._last_flush = time.monotonic()

    def run(self) -> None:
        log.info("dhcp_log_shipper_starting", path=str(self.path))
        while not self._stop.is_set():
            if self._fh is None:
                if not self._open():
                    self._stop.wait(timeout=FILE_WAIT_INTERVAL)
                    continue
            self._read_available()
            self._check_rotation()
            if self._should_flush():
                self._flush()
            self._stop.wait(timeout=TAIL_POLL_INTERVAL)
        if self._buffer:
            self._flush()
        self._close()
        log.info("dhcp_log_shipper_stopped")


__all__ = ["LogShipper", "DEFAULT_DHCP_LOG_PATH"]
