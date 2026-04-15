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

from .bootstrap import ensure_token
from .config import AgentConfig
from .drivers.base import DriverBase
from .drivers.bind9 import Bind9Driver
from .heartbeat import HeartbeatClient
from .sync import SyncLoop

log = structlog.get_logger(__name__)


def _select_driver(cfg: AgentConfig) -> DriverBase:
    if cfg.driver == "bind9":
        return Bind9Driver(state_dir=cfg.state_dir)
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
    for t in threads:
        t.start()

    stopping = threading.Event()

    def _sig(_signum, _frame):  # noqa: ANN001
        log.info("dns_agent_signal_received")
        stopping.set()
        heartbeat.stop()
        syncer.stop()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stopping.is_set():
        time.sleep(1.0)
        if not driver.daemon_running() and cfg.driver == "bind9":
            log.error("dns_daemon_exited", driver=cfg.driver)
            return 2

    log.info("dns_agent_exiting")
    return 0


def main_entry() -> int:
    cfg = AgentConfig.from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main_entry())
