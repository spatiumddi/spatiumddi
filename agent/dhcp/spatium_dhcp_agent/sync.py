"""Config long-poll loop for the DHCP agent.

Hits ``GET /api/v1/dhcp/agents/config`` with ``If-None-Match``. On 200 the
agent:

1. atomically writes the new bundle to the on-disk cache,
2. renders the bundle into a combined Kea ``{"Dhcp4": ..., "Dhcp6": ...}``
   document,
3. SPLITS it and atomically writes ``{"Dhcp4": ...}`` to ``KEA_CONFIG_PATH``
   and ``{"Dhcp6": ...}`` to ``KEA_CONFIG_PATH_V6`` — they MUST be separate
   files because ``kea-dhcp4 -t`` rejects a stray ``Dhcp6`` key (and v.v.),
4. asks BOTH kea-dhcp4 and kea-dhcp6 to reload via their control sockets.

On 304 the loop just continues. The control plane holds the connection open
for ~LONGPOLL_TIMEOUT seconds, so this is cheap.

Non-negotiable #5: after three consecutive failed polls the agent switches
to "offline mode" — logs a single warning, backs off to one retry per 60s,
and keeps serving from cache.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from .cache import load_config, save_config, save_rendered_kea, save_token
from .config import AgentConfig
from .kea_ctrl import KeaCtrlError, config_reload, config_test
from .render_kea import render as render_kea

log = structlog.get_logger(__name__)


def _touch_ready_marker(state_dir: Path) -> None:
    """Stamp ``<state_dir>/.ready`` after the first successful sync (#296 A2).

    The K8s DaemonSet readinessProbe execs a marker-file check + a light
    daemon ping; the marker representing "I have synced at least once" plus
    the hostPath bundle cache lets a pod that restarts into warm state be
    Ready immediately. Idempotent — ``touch`` on an existing file is fine
    and a no-op once stamped. Caller MUST only invoke after a successful
    fetch + persist + driver-apply; a failed sync must not flip readiness
    true. Best-effort: a marker write that races a filesystem error never
    blocks the daemon — we just log and move on, and the next successful
    apply retries.
    """
    try:
        marker = state_dir / ".ready"
        marker.touch(exist_ok=True)
    except OSError:
        log.exception("ready_marker_touch_failed", path=str(state_dir / ".ready"))


_FAILURE_THRESHOLD = 3  # DHCP.md §6: offline after 3 consecutive failures
_OFFLINE_RETRY_SECONDS = 60.0
# Bootstrap reload races Kea's own startup — entrypoint launches kea-dhcp4
# in the background just before the agent, so the control socket may not
# exist for up to a second or two. Retry reload until the socket answers
# or we give up. After ``_BOOTSTRAP_RELOAD_TIMEOUT`` we let Kea keep
# running on its pre-boot config and rely on the next real config change
# (or an operator restart) to pick up the new render.
_BOOTSTRAP_RELOAD_TIMEOUT = 15.0
_BOOTSTRAP_RELOAD_INTERVAL = 1.0


class SyncLoop:
    def __init__(
        self,
        cfg: AgentConfig,
        token_ref: list[str],
        heartbeat: Any,
        ha_poller: Any | None = None,
        peer_watcher: Any | None = None,
    ):
        self.cfg = cfg
        self.token_ref = token_ref
        self.heartbeat = heartbeat
        self.ha_poller = ha_poller
        self.peer_watcher = peer_watcher
        self._stop = threading.Event()
        self._current_etag: str | None = None
        self._consecutive_failures = 0
        self._offline = False

        # Preload cached bundle — offline-operation guarantee.
        #
        # Kea was just launched by the entrypoint with the Dockerfile-baked
        # config and will not pick up the rendered bundle unless we issue a
        # config-reload. Retry the reload for a few seconds to cover Kea's
        # own startup (socket may not exist yet). Without this retry, if
        # the control plane later returns 304 on /config, Kea would stay
        # on its baked config forever — in particular losing HA state on
        # any agent restart.
        bundle, etag = load_config(self.cfg.state_dir)
        if bundle is not None:
            self._current_etag = etag
            try:
                self._apply_bundle(
                    bundle,
                    reload_kea=True,
                    reload_retry_timeout=_BOOTSTRAP_RELOAD_TIMEOUT,
                )
                # #296 A2 — warm-restart readiness. The hostPath cache carries
                # the bundle we just successfully re-applied; the marker tells
                # the K8s readinessProbe this pod is ready to serve without
                # waiting for the next control-plane long-poll round-trip.
                _touch_ready_marker(self.cfg.state_dir)
                log.info("dhcp_agent_bootstrap_from_cache", etag=etag)
                # #170 Wave C1 — fleet-upgrade / reboot / SNMP / NTP
                # trigger-file writes moved to the supervisor's
                # heartbeat loop. The DHCP service container drops its
                # host bind mounts (``/etc/spatiumddi-host``,
                # ``/boot/efi-host``, ``/var/lib/spatiumddi-host/
                # release-state``, ``/run/udev``) in C1 so it can no
                # longer write the trigger surface anyway; the
                # supervisor's appliance-state module is the single
                # producer of appliance-host trigger files now.
            except Exception:
                log.exception("bootstrap_cache_apply_failed")

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.Client:
        # server holds for ~longpoll_timeout seconds, give client a bit more
        return httpx.Client(
            base_url=self.cfg.control_plane_url,
            verify=self.cfg.httpx_verify(),
            timeout=self.cfg.longpoll_timeout + 15.0,
        )

    @staticmethod
    def _atomic_write_json(path: Path, doc: dict[str, Any]) -> None:
        """Write ``doc`` to ``path`` via the temp-write-then-rename pattern.

        The rename is atomic on POSIX so a reader (or a crash mid-write)
        never sees a half-written Kea config.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
        tmp.replace(path)

    def _reload_socket(
        self,
        socket_path: Path,
        config_doc: dict[str, Any],
        daemon: str,
        reload_retry_timeout: float,
    ) -> bool:
        """Preflight (config-test) then reload one Kea daemon.

        Returns ``True`` on a successful reload, ``False`` otherwise. Never
        raises, so a v6 failure can't abort the v4 apply (and vice-versa). Two
        failure modes are now distinguished (#477):

        * **Config rejected** — config-test / reload answers with a non-zero
          result. Terminal (retrying won't fix a bad render), so surface Kea's
          *actual* error text in ``daemon_status`` and skip the reload rather
          than disturb a running daemon with a config it will reject. This is
          what turns an opaque "degraded" into "pool 10.0.0.0/24 is not part
          of the subnet …".
        * **Socket not ready** — an ``OSError`` connecting to the control
          socket during Kea's startup window. Retry until the deadline.
        """
        deadline = time.monotonic() + reload_retry_timeout
        last_err: Exception | None = None
        while True:
            try:
                # config-test validates WITHOUT applying and returns the real
                # reason on rejection; only reload once it passes.
                config_test(socket_path, config_doc)
                config_reload(socket_path)
                return True
            except (KeaCtrlError, OSError) as e:
                # Retry BOTH classes through the deadline. An OSError is the
                # control socket not being up yet; and during Kea's startup
                # window the command channel can answer config-test with a
                # *transient* KeaCtrlError (empty / non-JSON / not-ready) that a
                # moment later succeeds — so a rejection is only treated as
                # terminal once the retry window is exhausted. In the steady-
                # state apply path reload_retry_timeout is 0, so a genuinely bad
                # config still reports immediately without a spurious reload.
                last_err = e
                if time.monotonic() >= deadline:
                    break
                log.debug(
                    "kea_reload_socket_retry",
                    daemon=daemon,
                    error=str(e),
                    wait=_BOOTSTRAP_RELOAD_INTERVAL,
                )
                if self._stop.wait(_BOOTSTRAP_RELOAD_INTERVAL):
                    break
        # Deadline exhausted — report the reason that fits the last error: a
        # config Kea rejected (surface its text) vs a socket we never reached.
        if isinstance(last_err, KeaCtrlError):
            log.warning("kea_config_rejected", daemon=daemon, error=str(last_err))
            self.heartbeat.daemon_status = {
                "status": "degraded",
                "reason": f"{daemon}_config_rejected: {last_err}",
            }
        else:
            log.warning("kea_config_reload_failed", daemon=daemon, error=str(last_err))
            self.heartbeat.daemon_status = {
                "status": "degraded",
                "reason": f"{daemon}_socket_unreachable: {last_err}",
            }
        return False

    def _apply_bundle(
        self,
        bundle: dict[str, Any],
        *,
        reload_kea: bool,
        reload_retry_timeout: float = 0.0,
    ) -> None:
        """Render bundle → write Kea config → reload daemon.

        The control-plane long-poll returns an envelope ``{server_id,
        etag, bundle: {...}, pending_ops}``. The render expects the
        inner dict. Fall back to the envelope if ``bundle`` isn't
        there so cached v0 responses (pre-envelope) still render.
        """
        # Issue #258 — explicit narrowing. Pre-#258 the inline
        # ternary fell through to ``bundle`` for ANY non-dict value
        # of ``bundle["bundle"]`` (list, str, None, …) and the
        # downstream ``render_kea`` would render against the
        # envelope shape, producing a blank ``subnet4: []`` config.
        # Now we narrow explicitly: only the dict shape becomes
        # ``inner``; anything else (including a v0 cached response
        # with no envelope wrapper) falls back to the outer bundle
        # only when it is itself a dict.
        inner_candidate = bundle.get("bundle")
        if isinstance(inner_candidate, dict):
            inner = inner_candidate
        elif isinstance(bundle, dict):
            inner = bundle
        else:
            log.warning(
                "dhcp_sync_unexpected_bundle_shape",
                outer_type=type(bundle).__name__,
                inner_type=type(inner_candidate).__name__,
            )
            return
        # ``leases6`` mirrors the v4 lease file with the family digit
        # swapped (``kea-leases4.csv`` → ``kea-leases6.csv``) so the v6
        # daemon never writes the v4 lease store.
        lease_file_v6 = str(self.cfg.kea_lease_file).replace("leases4", "leases6")
        rendered = render_kea(
            inner,
            control_socket=str(self.cfg.kea_control_socket),
            lease_file=str(self.cfg.kea_lease_file),
            control_socket_v6=str(self.cfg.kea_control_socket_v6),
            lease_file_v6=lease_file_v6,
        )
        # Keep the HA poller aligned with whether Kea is about to load
        # the HA hook — when the bundle has no failover block the hook
        # won't be loaded, so we don't want the poller spamming
        # ha-status-get (and logging errors) against a daemon that
        # won't answer.
        if self.ha_poller is not None:
            self.ha_poller.set_enabled(bool(inner.get("failover")))

        # Split the combined render into the two daemon-specific files.
        # kea-dhcp4 rejects a doc containing a stray ``Dhcp6`` key (and
        # kea-dhcp6 a stray ``Dhcp4``), so each file carries exactly one
        # top-level block. ``render_kea`` always emits both blocks (the
        # Dhcp6 one is an idle skeleton when there are no v6 scopes).
        dhcp4_doc = {"Dhcp4": rendered.get("Dhcp4", {})}
        dhcp6_doc = {"Dhcp6": rendered.get("Dhcp6", {})}

        # Write the combined render to rendered/ (for audit/debug) and
        # then atomically write each split doc to its live config path.
        save_rendered_kea(self.cfg.state_dir, rendered)
        self._atomic_write_json(self.cfg.kea_config_path, dhcp4_doc)
        self._atomic_write_json(self.cfg.kea_config_path_v6, dhcp6_doc)
        log.info(
            "dhcp_config_written",
            path=str(self.cfg.kea_config_path),
            path_v6=str(self.cfg.kea_config_path_v6),
            subnets=len(dhcp4_doc["Dhcp4"].get("subnet4", [])),
            subnets_v6=len(dhcp6_doc["Dhcp6"].get("subnet6", [])),
        )
        if reload_kea:
            # Reload both daemons independently. A v6 reload failure must
            # not abort the v4 apply (and vice-versa) — each is wrapped in
            # the same retry / tolerate-missing-socket logic, and the
            # heartbeat daemon_status reflects the worst of the two.
            ok4 = self._reload_socket(
                self.cfg.kea_control_socket, dhcp4_doc, "dhcp4", reload_retry_timeout
            )
            ok6 = self._reload_socket(
                self.cfg.kea_control_socket_v6, dhcp6_doc, "dhcp6", reload_retry_timeout
            )
            if ok4 and ok6:
                self.heartbeat.daemon_status = {"status": "ok"}
        # Feed the peer-resolve watcher the bundle so it can track
        # hostname → IP drift and trigger a re-render if any peer's
        # IP changes. No-op when the bundle has no failover block.
        if self.peer_watcher is not None:
            try:
                self.peer_watcher.set_bundle(bundle)
            except Exception:  # noqa: BLE001 — defensive; never block apply
                log.exception("peer_watcher_set_bundle_failed")

    def _record_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        if not self._offline and self._consecutive_failures >= _FAILURE_THRESHOLD:
            self._offline = True
            log.warning(
                "control_plane_unreachable",
                reason=reason,
                action="operating_from_cached_config",
            )

    def _record_success(self) -> None:
        if self._offline:
            log.info("control_plane_reconnected")
        self._offline = False
        self._consecutive_failures = 0

    def _poll_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.token_ref[0]}"}
        if self._current_etag:
            headers["If-None-Match"] = self._current_etag
        try:
            with self._client() as c:
                resp = c.get("/api/v1/dhcp/agents/config", headers=headers)
        except httpx.HTTPError as e:
            self._record_failure(f"http_error:{e}")
            log.warning("sync_http_error", error=str(e), offline=self._offline)
            self._stop.wait(_OFFLINE_RETRY_SECONDS if self._offline else 5.0)
            return

        if resp.status_code == 304:
            self._record_success()
            return
        if resp.status_code in (401, 404):
            # 401 = token expired/invalid. 404 = server row was deleted on the
            # control plane (user removed it in the UI); re-register to create
            # a fresh row rather than 404-looping forever.
            log.warning(
                "sync_will_rebootstrap",
                status=resp.status_code,
                reason="token_invalid" if resp.status_code == 401 else "server_missing",
            )
            save_token(self.cfg.state_dir, "")
            self._stop.set()
            return
        if resp.status_code != 200:
            self._record_failure(f"status:{resp.status_code}")
            log.warning("sync_unexpected_status", status=resp.status_code)
            self._stop.wait(_OFFLINE_RETRY_SECONDS if self._offline else 5.0)
            return

        try:
            bundle = resp.json()
        except ValueError:
            self._record_failure("invalid_json")
            log.warning("sync_invalid_json")
            self._stop.wait(_OFFLINE_RETRY_SECONDS if self._offline else 5.0)
            return

        if bundle.get("pending_approval"):
            log.info("sync_pending_approval_waiting")
            self._stop.wait(10.0)
            self._record_success()
            return

        etag = bundle.get("etag") or resp.headers.get("ETag")
        if not etag:
            log.warning("sync_bundle_missing_etag")
            self._record_success()
            return

        save_config(self.cfg.state_dir, bundle, etag)

        # #170 Wave C1 — fleet-upgrade / reboot / SNMP / NTP trigger
        # writes moved to the supervisor's heartbeat loop. The
        # ConfigBundle's ``fleet_upgrade`` / ``snmp_settings`` /
        # ``ntp_settings`` blocks remain in the wire shape for
        # backwards compatibility with pre-C1 in-field agents; C1+
        # DHCP service containers ignore them because the supervisor's
        # appliance_state module is the only producer of appliance-
        # host trigger files now.

        try:
            self._apply_bundle(bundle, reload_kea=True)
        except Exception as e:
            log.exception("sync_apply_failed")
            self.heartbeat.daemon_status = {
                "status": "degraded",
                "reason": f"config_apply_failed: {e}",
            }
            return

        self._current_etag = etag
        self._record_success()
        log.info("dhcp_config_applied", etag=etag)

        # #296 A2 — stamp readiness marker AFTER the bundle was fetched,
        # persisted to the hostPath cache, and the Kea reload succeeded.
        # A failed apply returns early above so we never reach this point
        # on error.
        _touch_ready_marker(self.cfg.state_dir)

        # Ack any pending ops included in the bundle so the control plane
        # stops re-delivering them on the next long-poll.
        for op in bundle.get("pending_ops") or []:
            op_id = op.get("op_id")
            if op_id:
                self.heartbeat.pending_acks.append({"op_id": op_id, "result": "ok"})

    def run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            # Safety net: cap poll rate even if the server returns 200s
            # back-to-back (bad bundle state, clock skew, etc.). The long-poll
            # blocks ~30s when etag matches, so this doesn't add latency in
            # the normal case.
            self._stop.wait(1.0)
