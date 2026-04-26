"""Supervisor — runs sync + heartbeat + lease-watcher threads.

kea-dhcp4 itself runs as a sibling process under tini in the container; the
agent supervises its own tasks and reloads Kea via the control socket. If any
thread crashes (and doesn't recover), the process exits non-zero so the
container orchestrator restarts us.
"""

from __future__ import annotations

import signal
import sys
import threading
import time

import structlog

from .bootstrap import ensure_token
from .config import AgentConfig
from .ha_status import HAStatusPoller
from .heartbeat import HeartbeatClient
from .leases import LeaseWatcher
from .log_shipper import LogShipper
from .metrics import MetricsPoller
from .peer_resolve import PeerResolveWatcher
from .sync import SyncLoop

log = structlog.get_logger(__name__)


def run(cfg: AgentConfig) -> int:
    _agent_id, token = ensure_token(cfg)
    token_ref = [token]

    heartbeat = HeartbeatClient(cfg, token_ref)
    ha_poller = HAStatusPoller(cfg, token_ref)
    # Placeholder watcher — we need the syncer to exist before we can
    # give the watcher its apply callback. Construct both and wire.
    syncer_holder: list[SyncLoop] = []
    peer_watcher = PeerResolveWatcher(
        apply_fn=lambda bundle, reload_kea=True: syncer_holder[0]._apply_bundle(
            bundle, reload_kea=reload_kea
        )
    )
    syncer = SyncLoop(
        cfg, token_ref, heartbeat, ha_poller=ha_poller, peer_watcher=peer_watcher
    )
    syncer_holder.append(syncer)
    leases = LeaseWatcher(cfg, token_ref, heartbeat)
    metrics = MetricsPoller(cfg, token_ref)
    log_shipper = LogShipper(cfg, token_ref)

    threads = [
        threading.Thread(target=syncer.run, name="sync", daemon=True),
        threading.Thread(target=heartbeat.run, name="heartbeat", daemon=True),
        threading.Thread(target=leases.run, name="leases", daemon=True),
        threading.Thread(target=ha_poller.run, name="ha-status", daemon=True),
        threading.Thread(target=peer_watcher.run, name="peer-resolve", daemon=True),
        threading.Thread(target=metrics.run, name="metrics", daemon=True),
        threading.Thread(target=log_shipper.run, name="log-shipper", daemon=True),
    ]
    for t in threads:
        t.start()

    stopping = threading.Event()

    def _sig(_signum, _frame):  # noqa: ANN001
        log.info("dhcp_agent_signal_received")
        stopping.set()
        heartbeat.stop()
        syncer.stop()
        leases.stop()
        ha_poller.stop()
        peer_watcher.stop()
        metrics.stop()
        log_shipper.stop()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stopping.is_set():
        time.sleep(1.0)
        # If any critical thread died, bubble up so the container restarts.
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            log.error("dhcp_agent_thread_died", threads=dead)
            return 2

    log.info("dhcp_agent_exiting")
    return 0


def main_entry() -> int:
    cfg = AgentConfig.from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main_entry())
