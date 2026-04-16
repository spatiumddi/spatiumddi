"""Heartbeat loop — sends liveness + op ACKs, rotates token if requested."""

from __future__ import annotations

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
        # token_ref is a 1-element list so the sync loop can swap the token in place
        self.cfg = cfg
        self.token_ref = token_ref
        self._stop = threading.Event()
        self.pending_acks: list[dict[str, Any]] = []
        self.daemon_status: dict[str, Any] = {}
        self.zone_serials: dict[str, int] = {}
        self.failed_ops_count = 0

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
            "daemon": self.daemon_status,
            "config": {},
            "ops_ack": self.pending_acks,
            "failed_ops_count": self.failed_ops_count,
            "zone_serials": self.zone_serials,
        }
        try:
            with self._client() as c:
                resp = c.post(
                    "/api/v1/dns/agents/heartbeat",
                    json=body,
                    headers={"Authorization": f"Bearer {self.token_ref[0]}"},
                )
            if resp.status_code == 200:
                data = resp.json()
                self.pending_acks.clear()
                rotated = data.get("rotated_token")
                if rotated:
                    self.token_ref[0] = rotated
                    save_token(self.cfg.state_dir, rotated)
                    log.info("dns_agent_token_rotated")
            else:
                log.warning("heartbeat_failed", status=resp.status_code)
        except httpx.HTTPError as e:
            log.warning("heartbeat_http_error", error=str(e))

    def run(self) -> None:
        while not self._stop.is_set():
            self.send_once()
            # ±3s jitter on the 30s interval
            interval = self.cfg.heartbeat_interval + random.uniform(-3, 3)
            self._stop.wait(timeout=max(5.0, interval))
