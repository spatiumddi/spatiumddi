"""Lease watcher — tail the Kea memfile CSV and batch-post lease events.

Kea's ``lease_cmds`` hook also supports programmatic queries; as a simple and
robust default we tail the lease file (``KEA_LEASE_FILE``). Events are flushed
to the control plane every 5 seconds or every 100 events, whichever comes first.

Kea memfile CSV format (v4)::

    address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,
    hostname,state,user_context,hwtype,hwaddr_source,pool_id

``state`` values: 0=default (active), 1=declined, 2=expired-reclaimed.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

from .config import AgentConfig

log = structlog.get_logger(__name__)

_BATCH_MAX_EVENTS = 100
_BATCH_MAX_SECONDS = 5.0

_STATE_MAP = {"0": "active", "1": "declined", "2": "expired"}


def _parse_row(row: list[str]) -> dict[str, Any] | None:
    if not row or row[0].startswith("address"):  # header or blank
        return None
    if len(row) < 10:
        return None
    try:
        ip = row[0].strip()
        mac = row[1].strip() or None
        valid_lifetime = int(row[3]) if row[3] else 0
        expire_epoch = int(row[4]) if row[4] else 0
        hostname = row[8].strip() or None
        state = _STATE_MAP.get(row[9].strip(), "active")
        starts_at = (
            datetime.fromtimestamp(expire_epoch - valid_lifetime, tz=timezone.utc).isoformat()
            if expire_epoch and valid_lifetime
            else None
        )
        ends_at = (
            datetime.fromtimestamp(expire_epoch, tz=timezone.utc).isoformat()
            if expire_epoch
            else None
        )
        return {
            "ip": ip,
            "mac": mac,
            "hostname": hostname,
            "state": state,
            "starts_at": starts_at,
            "ends_at": ends_at,
        }
    except (ValueError, IndexError):
        return None


class LeaseWatcher:
    def __init__(self, cfg: AgentConfig, token_ref: list[str], heartbeat: Any):
        self.cfg = cfg
        self.token_ref = token_ref
        self.heartbeat = heartbeat
        self._stop = threading.Event()
        self._pending: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._offset = 0

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        verify: bool | str = True
        if self.cfg.insecure_skip_tls_verify:
            verify = False
        elif self.cfg.tls_ca_path:
            verify = self.cfg.tls_ca_path
        return httpx.Client(base_url=self.cfg.control_plane_url, verify=verify, timeout=15.0)

    def _flush(self) -> None:
        if not self._pending:
            self._last_flush = time.monotonic()
            return
        body = {"events": self._pending}
        try:
            with self._client() as c:
                resp = c.post(
                    "/api/v1/dhcp/agents/lease-events",
                    json=body,
                    headers={"Authorization": f"Bearer {self.token_ref[0]}"},
                )
            if resp.status_code in (200, 202, 204):
                log.info("lease_events_flushed", count=len(self._pending))
                self.heartbeat.lease_count_since_start += len(self._pending)
                self._pending.clear()
            else:
                log.warning("lease_events_failed", status=resp.status_code)
        except httpx.HTTPError as e:
            log.warning("lease_events_http_error", error=str(e))
        self._last_flush = time.monotonic()

    def _read_new_rows(self, path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            # File rotated / truncated by Kea LFC
            self._offset = 0
        if size == self._offset:
            return []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(self._offset)
            data = f.read()
            self._offset = f.tell()
        if not data:
            return []
        reader = csv.reader(io.StringIO(data))
        return list(reader)

    def run(self) -> None:
        while not self._stop.is_set():
            rows = self._read_new_rows(self.cfg.kea_lease_file)
            for row in rows:
                evt = _parse_row(row)
                if evt is not None:
                    self._pending.append(evt)
                if len(self._pending) >= _BATCH_MAX_EVENTS:
                    self._flush()
            if (time.monotonic() - self._last_flush) >= _BATCH_MAX_SECONDS:
                self._flush()
            self._stop.wait(1.0)
        # Final flush on shutdown (best effort).
        self._flush()
