"""Supervisor — starts gobgpd, then runs sync + RIB-poll + heartbeat threads.

Mirrors the DNS agent's ``supervisor.py``: the Python agent process owns
the daemon subprocess directly (``gobgp.start_daemon`` — there is exactly
one daemon to manage here, same as BIND9's single ``named``), rather than
a shell-level supervise loop. If gobgpd dies, or any agent thread dies
(and doesn't recover — a 401/404 sets its own stop event, see ``sync.py``
/ ``heartbeat.py``), the process exits non-zero so the container
orchestrator restarts everything, which re-bootstraps from the cached PSK
and re-launches gobgpd from the last-known-good on-disk config (non-
negotiable #5).
"""

from __future__ import annotations

import signal
import sys
import threading
import time

import structlog

from . import gobgp
from .bootstrap import ensure_token
from .config import AgentConfig
from .heartbeat import HeartbeatClient
from .rib import RibPoller
from .sync import SyncLoop

log = structlog.get_logger(__name__)


def run(cfg: AgentConfig) -> int:
    _agent_id, token = ensure_token(cfg)
    token_ref = [token]

    proc = gobgp.start_daemon(cfg)

    heartbeat = HeartbeatClient(cfg, token_ref)
    rib = RibPoller(cfg, token_ref, heartbeat)
    syncer = SyncLoop(cfg, token_ref, heartbeat, rib, proc)

    threads = [
        threading.Thread(target=syncer.run, name="sync", daemon=True),
        threading.Thread(target=rib.run, name="rib-poll", daemon=True),
        threading.Thread(target=heartbeat.run, name="heartbeat", daemon=True),
    ]
    for t in threads:
        t.start()

    stopping = threading.Event()

    def _sig(_signum, _frame):  # noqa: ANN001
        log.info("lg_agent_signal_received")
        stopping.set()
        heartbeat.stop()
        syncer.stop()
        rib.stop()
        if gobgp.daemon_running(proc):
            proc.terminate()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stopping.is_set():
        time.sleep(1.0)
        if not gobgp.daemon_running(proc):
            log.error("lg_gobgpd_exited")
            return 2
        # If any agent thread died (e.g. sync/heartbeat dropped its token
        # after a 401/404 and self-stopped), exit so the container
        # orchestrator restarts us — bootstrap then re-registers from PSK
        # with a fresh empty token cache.
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            log.error("lg_agent_thread_died", threads=dead)
            return 2

    log.info("lg_agent_exiting")
    return 0


def main_entry() -> int:
    cfg = AgentConfig.from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main_entry())
