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
from .heartbeat import HeartbeatClient
from .metrics import MetricsPoller
from .query_log_shipper import QueryLogShipper
from .sync import SyncLoop

log = structlog.get_logger(__name__)


def _select_driver(cfg: AgentConfig) -> DriverBase:
    if cfg.driver == "bind9":
        return Bind9Driver(state_dir=cfg.state_dir)
    if cfg.driver == "powerdns":
        return PowerDNSDriver(state_dir=cfg.state_dir)
    raise RuntimeError(f"Unknown driver: {cfg.driver}")


def run(cfg: AgentConfig) -> int:
    # Bootstrap / token
    _agent_id, token = ensure_token(cfg)
    token_ref = [token]

    driver = _select_driver(cfg)
    heartbeat = HeartbeatClient(cfg, token_ref)
    syncer = SyncLoop(cfg, token_ref, driver, heartbeat)

    # Spawn daemon before threads so the first poll can reload it if needed
    driver.start_daemon()

    threads = [
        threading.Thread(target=syncer.run, name="sync", daemon=True),
        threading.Thread(target=heartbeat.run, name="heartbeat", daemon=True),
    ]

    # BIND9-specific telemetry / admin threads. PowerDNS exposes its
    # own statistics + log surfaces (Phase 2/3 work) — skipping the
    # BIND-specific threads avoids spurious errors on a PowerDNS
    # daemon that doesn't speak rndc / statistics-channels XML.
    metrics: MetricsPoller | None = None
    query_log: QueryLogShipper | None = None
    rndc_status: RndcStatusPoller | None = None
    if cfg.driver == "bind9":
        metrics = MetricsPoller(cfg, token_ref)
        query_log = QueryLogShipper(cfg, token_ref)
        rndc_status = RndcStatusPoller(cfg, token_ref)
        threads.extend(
            [
                threading.Thread(target=metrics.run, name="metrics", daemon=True),
                threading.Thread(target=query_log.run, name="query-log", daemon=True),
                threading.Thread(target=rndc_status.run, name="rndc-status", daemon=True),
            ]
        )

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

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    # Every supported driver in this image manages its own daemon
    # process (BIND9, PowerDNS). If the daemon dies, we exit non-zero
    # so the orchestrator restarts the container — agent + daemon are
    # bound lifecycle-wise.
    daemon_managed_drivers = {"bind9", "powerdns"}
    while not stopping.is_set():
        time.sleep(1.0)
        if cfg.driver in daemon_managed_drivers and not driver.daemon_running():
            log.error("dns_daemon_exited", driver=cfg.driver)
            return 2
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
