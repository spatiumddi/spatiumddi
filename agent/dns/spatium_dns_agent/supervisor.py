"""Supervisor — runs the DNS daemon + agent sync/heartbeat threads under tini.

If any child dies, signal everyone to stop and exit non-zero. The container
orchestrator (Docker / K8s) restarts us.
"""

from __future__ import annotations

import signal
import sys
import threading
import time

import structlog

from .admin_pusher import RndcStatusPoller
from .bootstrap import ensure_token
from .config import AgentConfig
from .drivers.base import DriverBase
from .drivers.bind9 import Bind9Driver
from .drivers.powerdns import PowerDNSDriver
from .drivers.technitium import TechnitiumDriver
from .heartbeat import HeartbeatClient
from .ingest import IngestWorker
from .metrics import MetricsPoller
from .query_log_shipper import QueryLogShipper
from .sync import SyncLoop

log = structlog.get_logger(__name__)


def _select_driver(cfg: AgentConfig) -> DriverBase:
    if cfg.driver == "bind9":
        return Bind9Driver(state_dir=cfg.state_dir)
    if cfg.driver == "powerdns":
        return PowerDNSDriver(state_dir=cfg.state_dir)
    if cfg.driver == "technitium":
        return TechnitiumDriver(state_dir=cfg.state_dir)
    raise RuntimeError(f"Unknown driver: {cfg.driver}")


def run(cfg: AgentConfig) -> int:
    # Bootstrap / token
    _agent_id, token = ensure_token(cfg)
    token_ref = [token]

    driver = _select_driver(cfg)
    heartbeat = HeartbeatClient(cfg, token_ref, driver=driver)
    syncer = SyncLoop(cfg, token_ref, driver, heartbeat)

    # Spawn daemon before threads so the first poll can reload it if needed
    driver.start_daemon()

    threads = [
        threading.Thread(target=syncer.run, name="sync", daemon=True),
        threading.Thread(target=heartbeat.run, name="heartbeat", daemon=True),
    ]

    # BIND9-specific telemetry / admin threads (statistics-channels
    # XML, rndc status). PowerDNS exposes its own surfaces via the
    # REST API instead, so we skip those threads on a PowerDNS
    # daemon to avoid spurious errors. The query-log shipper is
    # driver-agnostic — both BIND9 and PowerDNS write a log file
    # (BIND via ``query_log_file`` directive, PowerDNS via the
    # agent's stderr-to-file capture in ``start_daemon``); the
    # control plane dispatches parsing by ``server.driver`` so the
    # ingest endpoint accepts either format.
    metrics: MetricsPoller | None = None
    query_log: QueryLogShipper | None = None
    rndc_status: RndcStatusPoller | None = None
    ingest: IngestWorker | None = None
    if cfg.driver == "bind9":
        metrics = MetricsPoller(cfg, token_ref)
        query_log = QueryLogShipper(cfg, token_ref)
        rndc_status = RndcStatusPoller(cfg, token_ref)
        # Ingest-back for externally-injected DDNS records (issue #641).
        # BIND9 only — AXFRs dynamic zones from loopback and ships unknown
        # records to the control plane.
        ingest = IngestWorker(cfg, token_ref)
        threads.extend(
            [
                threading.Thread(target=metrics.run, name="metrics", daemon=True),
                threading.Thread(target=query_log.run, name="query-log", daemon=True),
                threading.Thread(
                    target=rndc_status.run, name="rndc-status", daemon=True
                ),
                threading.Thread(target=ingest.run, name="ingest", daemon=True),
            ]
        )
    elif cfg.driver == "powerdns":
        # PowerDNS log file is created by ``start_daemon`` redirecting
        # ``pdns_server`` stderr into the agent's own state dir
        # (so the unprivileged ``spatium`` user can write it).
        # Override via ``DNS_QUERY_LOG_PATH`` env var in custom deploys.
        pdns_log_path = str(cfg.state_dir / "pdns.log")
        query_log = QueryLogShipper(cfg, token_ref, path=pdns_log_path)
        threads.append(
            threading.Thread(target=query_log.run, name="query-log", daemon=True),
        )
    # technitium: no query-log thread in v1 — Technitium's query logging is
    # API/DB-backed (``/api/logs/query*``), not a tailable text file like
    # BIND9's query_log_file or PowerDNS's redirected stderr capture. Wiring
    # it needs a poll-and-diff shipper, not the existing file-tailing
    # QueryLogShipper — deferred to a fast-follow phase.

    for t in threads:
        t.start()

    stopping = threading.Event()

    def _sig(_signum, _frame):  # noqa: ANN001
        log.info("dns_agent_signal_received")
        stopping.set()
        heartbeat.stop()
        syncer.stop()
        if metrics is not None:
            metrics.stop()
        if query_log is not None:
            query_log.stop()
        if rndc_status is not None:
            rndc_status.stop()
        if ingest is not None:
            ingest.stop()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    # Every supported driver in this image manages its own daemon
    # process (BIND9, PowerDNS, Technitium). If the daemon dies, we exit
    # non-zero so the orchestrator restarts the container — agent + daemon
    # are bound lifecycle-wise.
    #
    # "Dies" presumes it was launched. ``start_daemon()`` above returns
    # WITHOUT a daemon when no config has been rendered yet (BIND9
    # ``named_conf_missing_startup_deferred``, PowerDNS
    # ``pdns_conf_missing_startup_deferred``) and the sync loop launches it
    # from ``swap_and_reload`` once the first bundle lands. The first bind
    # pod on a freshly joined cluster member always boots that way — its
    # state dir is empty and the control plane has not built its bundle
    # yet — and reading that deferred start as a death made this loop
    # return 2 one tick after boot: kubelet then back-off-restarted the
    # container until the bundle happened to arrive inside the 1 s window
    # (#1056: ``dns_daemon_exited`` 1.0 s after the deferral, Last State
    # exit 2, restart count +2 on a member). So: wait while nothing has
    # been launched, exit only when a launched daemon is gone. The chart's
    # liveness probe (tcp :53) still bounds the wait if no bundle ever comes.
    daemon_managed_drivers = {"bind9", "powerdns", "technitium"}
    waiting_since: float | None = None
    while not stopping.is_set():
        time.sleep(1.0)
        # A stop requested during the tick (SIGTERM / SIGINT: a DaemonSet
        # rollout, a scale-down, an operator) has already stopped every
        # worker thread through ``_sig`` and may have taken the daemon with
        # it. Whatever the checks below would find dead now died because we
        # were told to stop — take the designed exit, not the crash exits,
        # so the container's last state reads 0 rather than "thread died"
        # / "daemon exited" (#1056: every rollout ended the old container
        # with exit 2 and a dns_agent_thread_died in its log).
        if stopping.is_set():
            break
        if cfg.driver in daemon_managed_drivers:
            if driver.daemon_running():
                if waiting_since is not None:
                    log.info(
                        "dns_daemon_launched_after_deferred_start",
                        driver=cfg.driver,
                        waited_s=round(time.monotonic() - waiting_since, 1),
                    )
                    waiting_since = None
            elif driver.daemon_launched():
                log.error("dns_daemon_exited", driver=cfg.driver)
                return 2
            elif waiting_since is None:
                waiting_since = time.monotonic()
                log.info(
                    "dns_daemon_start_deferred_waiting",
                    driver=cfg.driver,
                    note="start_daemon spawned nothing (no rendered config yet); "
                    "the sync loop launches the daemon after the first bundle",
                )
        # If any critical thread died (e.g. the sync loop dropped its
        # token after a 401/404 and self-stopped), exit so the container
        # orchestrator restarts us — bootstrap then re-registers from
        # PSK with a fresh empty token cache.
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            log.error("dns_agent_thread_died", threads=dead)
            return 2

    log.info("dns_agent_exiting")
    return 0


def main_entry() -> int:
    cfg = AgentConfig.from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main_entry())
