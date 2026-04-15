"""Heartbeat loop — liveness + daemon status + token rotation."""

from __future__ import annotations

import os
import random
import threading
from typing import Any

import httpx
import structlog

from . import __version__
from .cache import save_token
from .config import AgentConfig

log = structlog.get_logger(__name__)


class HeartbeatClient:
    def __init__(self, cfg: AgentConfig, token_ref: list[str]):
        self.cfg = cfg
        self.token_ref = token_ref
        self._stop = threading.Event()
        self.daemon_status: dict[str, Any] = {}
        self.lease_count_since_start = 0

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        verify: bool | str = True
        if self.cfg.insecure_skip_tls_verify:
            verify = False
        elif self.cfg.tls_ca_path:
            verify = self.cfg.tls_ca_path
        return httpx.Client(base_url=self.cfg.control_plane_url, verify=verify, timeout=15.0)

    def send_once(self) -> None:
        body = {
            "agent_version": __version__,
            "pid": os.getpid(),
            "status": self.daemon_status.get("status", "ok"),
            "daemon": self.daemon_status,
            "lease_count_since_start": self.lease_count_since_start,
        }
        try:
            with self._client() as c:
                resp = c.post(
                    "/api/v1/dhcp/agents/heartbeat",
                    json=body,
                    headers={"Authorization": f"Bearer {self.token_ref[0]}"},
                )
            if resp.status_code == 200:
                data = resp.json()
                rotated = data.get("rotated_token")
                if rotated:
                    self.token_ref[0] = rotated
                    save_token(self.cfg.state_dir, rotated)
                    log.info("dhcp_agent_token_rotated")
            else:
                log.warning("heartbeat_failed", status=resp.status_code)
        except httpx.HTTPError as e:
            log.warning("heartbeat_http_error", error=str(e))

    def run(self) -> None:
        while not self._stop.is_set():
            self.send_once()
            interval = self.cfg.heartbeat_interval + random.uniform(-3, 3)
            self._stop.wait(timeout=max(5.0, interval))
