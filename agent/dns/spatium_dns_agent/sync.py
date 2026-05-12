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

from .admin_pusher import push_rendered_config
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
        # Tracks the structural-only fingerprint of the last applied bundle.
        # We re-render config + reload the daemon only when this changes.
        # Record-only changes rotate the full etag (so we get 200 not 304)
        # but leave structural_etag alone — the agent then drains record ops
        # via RFC 2136 over loopback without bouncing the daemon.
        self._current_structural_etag: str | None = None

        # Preload cached bundle (offline-operation guarantee)
        bundle, etag = load_config(self.cfg.state_dir)
        if bundle is not None:
            self._current_etag = etag
            try:
                self.driver.apply_config(bundle)
                self._current_structural_etag = bundle.get("structural_etag")
                log.info("dns_agent_bootstrap_from_cache", etag=etag)
                # Push the rendered tree once at bootstrap so operators
                # get a Config-tab snapshot the moment the agent comes
                # up — without this, the snapshot only lands on the
                # next structural reload (could be hours away on a
                # quiet group).
                try:
                    push_rendered_config(self.cfg, self.token_ref[0])
                except Exception:
                    log.exception("rendered_config_bootstrap_push_failed")
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
        if resp.status_code in (401, 404):
            # 401 = token expired/invalid. 404 = the server row was deleted
            # on the control plane (e.g. operator wiped it, or a fresh
            # control-plane install with cached creds). Both recover by
            # the same path: drop cached token and re-bootstrap from PSK.
            log.warning(
                "sync_will_rebootstrap",
                status=resp.status_code,
                reason="token_invalid" if resp.status_code == 401 else "server_missing",
            )
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

        # Atomic-swap cache always (cache is the source of truth for restarts)
        save_config(self.cfg.state_dir, bundle, etag)

        # Phase 8f-4 — fleet upgrade trigger. The control plane stamps
        # desired_appliance_version on the server row via the Fleet
        # view; the bundle's ``fleet_upgrade`` block carries it down
        # here. If the desired version doesn't match what's installed
        # AND we're on an appliance AND no trigger is already pending,
        # write the slot-upgrade trigger file. The host-side
        # spatiumddi-slot-upgrade.path unit picks it up (same path as
        # the manual /appliance OS Image card).
        fleet = bundle.get("fleet_upgrade") or {}
        if fleet.get("desired_appliance_version"):
            from .slot_state import maybe_fire_fleet_upgrade
            fired = maybe_fire_fleet_upgrade(
                fleet.get("desired_appliance_version"),
                fleet.get("desired_slot_image_url"),
            )
            if fired:
                log.info(
                    "fleet_upgrade_triggered",
                    desired_version=fleet.get("desired_appliance_version"),
                )

        # Re-render + reload daemon ONLY when structural fingerprint changes.
        # Record CRUD bumps the full etag but not structural_etag, so the
        # daemon stays running and ops are applied incrementally below.
        new_structural = bundle.get("structural_etag")
        if new_structural != self._current_structural_etag:
            try:
                self.driver.apply_config(bundle)
            except Exception as e:
                log.exception("sync_apply_failed")
                self.heartbeat.daemon_status = {
                    **self.heartbeat.daemon_status,
                    "status": "degraded",
                    "reason": f"config_validation_failed: {e}",
                }
                return
            self._current_structural_etag = new_structural
            log.info("structural_reload_applied", structural_etag=new_structural)

            # Post the serials we just rendered so the control plane can
            # show per-server drift. Best-effort — a failed POST doesn't
            # roll back the apply (we already serve the new config).
            self._report_zone_state(bundle)

            # Push the on-disk rendered config snapshot so the Server
            # Detail modal's Config tab can show "what's actually live
            # right now" — operators no longer need to SSH in to verify.
            try:
                push_rendered_config(self.cfg, self.token_ref[0])
            except Exception:
                log.exception("rendered_config_push_failed")

        self._current_etag = etag

        # Drain pending record ops via RFC 2136 (no daemon reload)
        dnssec_states: list[dict[str, Any]] = []
        for op in bundle.get("pending_record_ops", []):
            try:
                result = self.driver.apply_record_op(op)
                self.heartbeat.pending_acks.append({"op_id": op["op_id"], "result": "ok"})
                log.info("record_op_applied", op_id=op["op_id"], op=op.get("op"),
                         zone=op.get("zone_name"))
                # PowerDNS DNSSEC ops return the DS rrset so we can ship
                # it back to the control plane in one batched POST below.
                if isinstance(result, dict) and "dnssec_state" in result:
                    dnssec_states.append(result["dnssec_state"])
            except Exception as e:
                log.exception("op_apply_failed", op_id=op.get("op_id"))
                self.heartbeat.pending_acks.append(
                    {"op_id": op["op_id"], "result": "error", "message": str(e)}
                )
                self.heartbeat.failed_ops_count += 1
        if dnssec_states:
            self._report_dnssec_state(dnssec_states)

    def _report_zone_state(self, bundle: dict[str, Any]) -> None:
        """POST ``{zones: [{zone_name, serial}, ...]}`` after a successful apply.

        Best-effort. A dead control plane or transient 5xx never blocks
        the daemon — the next structural reload will try again.
        """
        entries: list[dict[str, Any]] = []
        for z in bundle.get("zones") or []:
            name = z.get("name")
            serial = z.get("serial")
            if not name or serial is None:
                continue
            entries.append({"zone_name": str(name), "serial": int(serial)})
        if not entries:
            return
        headers = {"Authorization": f"Bearer {self.token_ref[0]}"}
        try:
            with self._client() as c:
                resp = c.post(
                    "/api/v1/dns/agents/zone-state",
                    headers=headers,
                    json={"zones": entries},
                )
            if resp.status_code != 200:
                log.warning(
                    "zone_state_report_non200",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
        except httpx.HTTPError as e:
            log.warning("zone_state_report_failed", error=str(e))

    def _report_dnssec_state(self, states: list[dict[str, Any]]) -> None:
        """POST the DS rrset(s) the driver just produced after a sign /
        unsign op (issue #127, Phase 3c.fe).

        Best-effort. A failed POST never blocks the apply — operators
        re-trigger sign in the UI to retry.
        """
        if not states:
            return
        headers = {"Authorization": f"Bearer {self.token_ref[0]}"}
        try:
            with self._client() as c:
                resp = c.post(
                    "/api/v1/dns/agents/dnssec-state",
                    headers=headers,
                    json={"zones": states},
                )
            if resp.status_code != 200:
                log.warning(
                    "dnssec_state_report_non200",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
        except httpx.HTTPError as e:
            log.warning("dnssec_state_report_failed", error=str(e))

    def run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
