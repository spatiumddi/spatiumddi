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
from .drivers.base import DriverBase

log = structlog.get_logger(__name__)


class HeartbeatClient:
    def __init__(
        self,
        cfg: AgentConfig,
        token_ref: list[str],
        driver: DriverBase | None = None,
    ):
        # token_ref is a 1-element list so the sync loop can swap the token in place
        self.cfg = cfg
        self.token_ref = token_ref
        self._stop = threading.Event()
        self.pending_acks: list[dict[str, Any]] = []
        self.daemon_status: dict[str, Any] = {}
        self.failed_ops_count = 0
        # #638 — the driver is only used to probe the DNS daemon's version.
        # Optional so tests (and any caller that just wants liveness) can build
        # a HeartbeatClient without a driver.
        self.driver = driver
        self._daemon_version_cached: str | None = None

    def _daemon_version(self) -> str | None:
        """DNS daemon version, probed once and cached.

        Same reasoning as the DHCP agent's ``_kea_version`` (#637): the daemon
        binary cannot change while this process lives, because a new image
        means a new container and a restarted agent. So probe until it answers,
        then stop — the probe forks a subprocess, and doing that every 30 s on
        the thread that carries our liveness signal buys nothing.
        """
        if self._daemon_version_cached is None and self.driver is not None:
            self._daemon_version_cached = self.driver.daemon_version()
        return self._daemon_version_cached

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        verify: bool | str = True
        if self.cfg.insecure_skip_tls_verify:
            verify = False
        elif self.cfg.tls_ca_path:
            verify = self.cfg.tls_ca_path
        return httpx.Client(
            base_url=self.cfg.control_plane_url, verify=verify, timeout=15.0
        )

    def send_once(self) -> None:
        body: dict[str, Any] = {
            "agent_version": __version__,
            # #638 — the DNS DAEMON's version (e.g. "5.0.5" / "9.20.26"),
            # distinct from ``agent_version`` (this python agent). The
            # rolling-upgrade preflight needs it: crossing PowerDNS 4.x → 5.x
            # performs a one-way LMDB schema migration, so the operator has to
            # be told before they press Start. None = probe failed; the control
            # plane must treat that as "unknown", never as a specific version.
            "daemon_version": self._daemon_version(),
            "daemon": self.daemon_status,
            "config": {},
            "ops_ack": self.pending_acks,
            "failed_ops_count": self.failed_ops_count,
        }
        # #170 Wave C1 — slot / deployment / upgrade-state telemetry
        # used to ship here per Phase 8f-2; now lives on the
        # supervisor's heartbeat (one producer instead of three).
        # The DNS service container drops the host bind mounts in C1
        # so it can no longer read those signals anyway.
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
            elif resp.status_code in (401, 404):
                # Issue #248 — mirror sync.py's 401/404 recovery path.
                # 401 = token expired / invalid; 404 = the server row
                # was deleted on the control plane. Both recover by
                # the same path: drop cached token + stop the thread.
                # The supervisor's dead-thread watch exits the
                # container; the next start re-bootstraps from PSK.
                # Pre-#248 the heartbeat just spam-warned until sync
                # noticed the same 401 (could be a full heartbeat
                # interval later).
                log.warning(
                    "heartbeat_will_rebootstrap",
                    status=resp.status_code,
                    reason=(
                        "token_invalid" if resp.status_code == 401 else "server_missing"
                    ),
                )
                save_token(self.cfg.state_dir, "")
                self._stop.set()
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
