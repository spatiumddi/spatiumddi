"""Heartbeat loop — liveness + daemon status + token rotation."""

from __future__ import annotations

import os
import random
import threading
from typing import Any

import httpx
import structlog

from . import __version__, kea_ctrl
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
        self.pending_acks: list[dict] = []

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.cfg.control_plane_url,
            verify=self.cfg.httpx_verify(),
            timeout=15.0,
        )

    def _kea_version(self) -> str | None:
        sock = getattr(self.cfg, "kea_control_socket", None)
        if sock is None:
            return None
        return kea_ctrl.version_get(sock)

    def send_once(self) -> None:
        # Drain queued op acks (queued by SyncLoop when bundle includes pending_ops).
        ops_ack: list[dict] = []
        while self.pending_acks:
            ops_ack.append(self.pending_acks.pop(0))
        body: dict[str, Any] = {
            "agent_version": __version__,
            "pid": os.getpid(),
            "status": self.daemon_status.get("status", "ok"),
            "daemon": self.daemon_status,
            "lease_count_since_start": self.lease_count_since_start,
            "ops_ack": ops_ack,
            # #637 — the running Kea daemon's version (e.g. "3.0.3"), read live
            # off the control socket. The rolling-upgrade preflight needs it:
            # Kea 3.0's HA hook cannot talk to a peer older than 2.7, so a
            # node-at-a-time upgrade across that boundary breaks HA mid-run.
            # None = daemon not up yet / didn't answer; the control plane must
            # treat that as "unknown", never as "old".
            "kea_version": self._kea_version(),
        }
        # #170 Wave C1 — slot / deployment / upgrade-state telemetry
        # used to ship here per Phase 8f-2; now lives on the
        # supervisor's heartbeat (one producer instead of three).
        # The DHCP service container drops the host bind mounts in C1
        # so it can no longer read those signals anyway.
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
            elif resp.status_code in (401, 404):
                # Token invalidated or server row deleted — clear token and
                # exit so supervisor restarts the container (→ re-bootstrap).
                log.warning("heartbeat_will_rebootstrap", status=resp.status_code)
                save_token(self.cfg.state_dir, "")
                self._stop.set()
            else:
                log.warning("heartbeat_failed", status=resp.status_code)
        except httpx.HTTPError as e:
            log.warning("heartbeat_http_error", error=str(e))

    def run(self) -> None:
        while not self._stop.is_set():
            self.send_once()
            interval = self.cfg.heartbeat_interval + random.uniform(-3, 3)
            self._stop.wait(timeout=max(5.0, interval))
