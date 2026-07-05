"""Config long-poll loop — fetches the peer-config bundle and re-renders
gobgpd's config on every change.

Cloned from ``agent/dns/spatium_dns_agent/sync.py``'s ``SyncLoop`` shape
(GET with ``If-None-Match``, preload+apply the cached bundle before the
first poll, atomic-swap to disk on 200, 401/404 -> clear token + stop).
Unlike the DNS agent there is no structural-vs-record-only split here —
every ``bgp_lg_peer`` change is a full peer-set re-render (GoBGP's
``apply_config`` is cheap: render + write + SIGHUP, no daemon restart), so
any 200 response with a new etag triggers :func:`spatium_lg_agent.gobgp.
apply_config`.

``GET /api/v1/looking-glass/agents/config`` (see
``backend/app/api/v1/looking_glass/agents.py::agent_config_longpoll``)
returns an envelope, not a bare bundle::

    {"collector_id": "...", "etag": "...", "bundle": {"collector_name": ...,
     "peers": [...]}}

This loop unwraps ``bundle`` before handing it to ``gobgp.py`` /
``rib.py`` — the cache on disk stores the unwrapped inner bundle (that's
what actually gets rendered), with the etag tracked alongside it. There is
no ``pending_approval`` gate here (unlike the DHCP/DNS agent protocol) —
the collector identity row has no approval flow, per the agents.py module
docstring.
"""

from __future__ import annotations

import random
import subprocess
import threading
import time
from typing import Any

import httpx
import structlog

from . import gobgp
from .cache import load_config, save_config, save_token
from .config import AgentConfig

log = structlog.get_logger(__name__)

# Jittered exponential backoff for a persistently-failing bundle apply
# (mirrors bootstrap.py's register loop). Bounded so a bad bundle can't
# hammer the /config long-poll or peg the CPU, but small enough that a
# transient failure recovers within a poll or two.
_APPLY_BACKOFF_BASE = 2.0
_APPLY_BACKOFF_CAP = 45.0


class SyncLoop:
    def __init__(
        self,
        cfg: AgentConfig,
        token_ref: list[str],
        heartbeat: Any,
        rib: Any,
        gobgpd_proc: "subprocess.Popen[bytes] | None",
    ):
        self.cfg = cfg
        self.token_ref = token_ref
        self.heartbeat = heartbeat
        self.rib = rib
        self.gobgpd_proc = gobgpd_proc
        self._stop = threading.Event()
        self._current_etag: str | None = None
        # Grows on consecutive apply failures, resets to base on success.
        self._apply_backoff = _APPLY_BACKOFF_BASE

        # Preload cached bundle (non-negotiable #5 — offline-operation
        # guarantee). Applying it BEFORE the first network poll is what
        # keeps already-configured BGP sessions up if the control plane
        # is unreachable at container start.
        bundle, etag = load_config(self.cfg.state_dir)
        if bundle is not None:
            self._current_etag = etag
            try:
                gobgp.apply_config(self.cfg, bundle, self.gobgpd_proc)
                self.rib.set_peers(
                    gobgp.peer_address_map(bundle),
                    gobgp.peer_import_scopes(bundle),
                )
                log.info("lg_agent_bootstrap_from_cache", etag=etag)
            except Exception:
                log.exception("lg_bootstrap_cache_apply_failed")

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        # Server holds the long-poll for ~cfg.longpoll_timeout; give the
        # client meaningfully more so a slow-but-alive control plane
        # doesn't look like a network error.
        return httpx.Client(
            base_url=self.cfg.control_plane_url,
            verify=self.cfg.httpx_verify(),
            timeout=60.0,
        )

    def _poll_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.token_ref[0]}"}
        if self._current_etag:
            headers["If-None-Match"] = self._current_etag
        try:
            with self._client() as c:
                resp = c.get("/api/v1/looking-glass/agents/config", headers=headers)
        except httpx.HTTPError as e:
            log.warning("lg_sync_http_error", error=str(e))
            time.sleep(5.0)
            return

        if resp.status_code == 304:
            return
        if resp.status_code in (401, 404):
            # 401 = token expired/invalid. 404 = the collector row was
            # deleted on the control plane. Both recover the same way:
            # drop the cached token and let the supervisor's dead-thread
            # detection restart the container -> re-bootstrap from PSK.
            log.warning(
                "lg_sync_will_rebootstrap",
                status=resp.status_code,
                reason=(
                    "token_invalid" if resp.status_code == 401 else "collector_missing"
                ),
            )
            save_token(self.cfg.state_dir, "")
            self._stop.set()
            return
        if resp.status_code != 200:
            log.warning("lg_sync_unexpected_status", status=resp.status_code)
            time.sleep(5.0)
            return

        envelope = resp.json()
        etag = envelope.get("etag") or resp.headers.get("ETag")
        if not etag:
            log.warning("lg_sync_bundle_missing_etag")
            return
        # Unwrap the envelope — ``bundle`` (collector_name + peers) is what
        # gobgp.py actually renders from; see module docstring.
        inner_bundle = envelope.get("bundle") or {}

        # Atomic-swap cache always — cache is the source of truth across
        # restarts, even before we know the apply below succeeds. We cache
        # the unwrapped inner bundle (matches what the constructor's
        # ``load_config`` preload path expects to hand to ``gobgp.py``).
        save_config(self.cfg.state_dir, inner_bundle, etag)

        try:
            gobgp.apply_config(self.cfg, inner_bundle, self.gobgpd_proc)
        except Exception as e:
            log.exception("lg_sync_apply_failed")
            self.heartbeat.daemon_status = {
                **self.heartbeat.daemon_status,
                "status": "degraded",
                "reason": f"config_render_failed: {e}",
            }
            # Bounded backoff so a persistently-bad bundle (e.g. a
            # ReceiveOnlyViolation, a malformed peer set) can't spin the
            # bare ``run()`` loop: we deliberately DON'T advance
            # ``_current_etag``, so the next poll re-fetches the SAME
            # bundle to retry the apply — but only after sleeping, instead
            # of immediately re-hitting /config with the stale
            # If-None-Match and pegging the CPU. ``_stop.wait`` so a
            # shutdown signal interrupts the sleep promptly.
            sleep_for = min(
                self._apply_backoff + random.uniform(0, 2), _APPLY_BACKOFF_CAP
            )
            log.warning("lg_sync_apply_backoff", seconds=round(sleep_for, 1))
            self._stop.wait(sleep_for)
            self._apply_backoff = min(self._apply_backoff * 2, _APPLY_BACKOFF_CAP)
            return

        # Apply succeeded — reset the backoff so the next failure (if any)
        # starts from the base again.
        self._apply_backoff = _APPLY_BACKOFF_BASE
        self.rib.set_peers(
            gobgp.peer_address_map(inner_bundle),
            gobgp.peer_import_scopes(inner_bundle),
        )
        self._current_etag = etag
        log.info(
            "lg_config_applied",
            etag=etag,
            peer_count=len(inner_bundle.get("peers") or []),
        )

    def run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
