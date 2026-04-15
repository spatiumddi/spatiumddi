"""Config long-poll loop + op execution.

Hits GET /dns/agents/config with If-None-Match; on 200 applies the new
bundle (atomic disk swap, daemon-specific reload) and dispatches any
pending_record_ops through the active driver. On 304 it just loops back.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import structlog

from .cache import load_config, save_config
from .config import AgentConfig
from .drivers.base import DriverBase

log = structlog.get_logger(__name__)


class SyncLoop:
    def __init__(self, cfg: AgentConfig, token_ref: list[str], driver: DriverBase, heartbeat: Any):
        self.cfg = cfg
        self.token_ref = token_ref
        self.driver = driver
        self.heartbeat = heartbeat
        self._stop = threading.Event()
        self._current_etag: str | None = None

        # Preload cached bundle (offline-operation guarantee)
        bundle, etag = load_config(self.cfg.state_dir)
        if bundle is not None:
            self._current_etag = etag
            try:
                self.driver.apply_config(bundle)
                log.info("dns_agent_bootstrap_from_cache", etag=etag)
            except Exception:
                log.exception("bootstrap_cache_apply_failed")

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        verify: bool | str = True
        if self.cfg.insecure_skip_tls_verify:
            verify = False
        elif self.cfg.tls_ca_path:
            verify = self.cfg.tls_ca_path
        # server holds for ~30s, give client a bit more
        return httpx.Client(base_url=self.cfg.control_plane_url, verify=verify, timeout=60.0)

    def _poll_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.token_ref[0]}"}
        if self._current_etag:
            headers["If-None-Match"] = self._current_etag
        try:
            with self._client() as c:
                resp = c.get("/api/v1/dns/agents/config", headers=headers)
        except httpx.HTTPError as e:
            log.warning("sync_http_error", error=str(e))
            time.sleep(5.0)
            return

        if resp.status_code == 304:
            return
        if resp.status_code == 401:
            log.warning("sync_token_expired_will_rebootstrap")
            # Drop cached token; next bootstrap round gets a new one
            from .cache import save_token
            save_token(self.cfg.state_dir, "")
            self._stop.set()
            return
        if resp.status_code != 200:
            log.warning("sync_unexpected_status", status=resp.status_code)
            time.sleep(5.0)
            return

        bundle = resp.json()
        if bundle.get("pending_approval"):
            log.info("sync_pending_approval_waiting")
            time.sleep(10.0)
            return

        etag = bundle.get("etag") or resp.headers.get("ETag")
        if not etag:
            log.warning("sync_bundle_missing_etag")
            return

        # Atomic-swap cache, then apply
        save_config(self.cfg.state_dir, bundle, etag)
        try:
            self.driver.apply_config(bundle)
        except Exception as e:
            log.exception("sync_apply_failed")
            # keep previous daemon state running, report via heartbeat
            self.heartbeat.daemon_status = {
                **self.heartbeat.daemon_status,
                "status": "degraded",
                "reason": f"config_validation_failed: {e}",
            }
            return

        self._current_etag = etag

        # Drain pending record ops
        for op in bundle.get("pending_record_ops", []) or []:
            try:
                self.driver.apply_record_op(op)
                self.heartbeat.pending_acks.append({"op_id": op["op_id"], "result": "ok"})
            except Exception as e:
                log.exception("op_apply_failed", op_id=op.get("op_id"))
                self.heartbeat.pending_acks.append(
                    {"op_id": op["op_id"], "result": "error", "message": str(e)}
                )
                self.heartbeat.failed_ops_count += 1

    def run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
