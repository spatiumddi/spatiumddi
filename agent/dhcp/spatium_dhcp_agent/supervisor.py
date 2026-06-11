"""Supervisor — runs sync + heartbeat + lease-watcher threads.

kea-dhcp4 itself runs as a sibling process under tini in the container; the
agent supervises its own tasks and reloads Kea via the control socket. If any
thread crashes (and doesn't recover), the process exits non-zero so the
container orchestrator restarts us.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

import structlog

from .bootstrap import ensure_token
from .config import AgentConfig
from .dhcp_fingerprint import DhcpFingerprintShipper
from .rogue_probe import RogueProbeShipper
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
    # Construct the watcher first (SyncLoop needs the reference in its
    # ``__init__``), then arm it once the SyncLoop exists. Issue #265 —
    # the old ``syncer_holder[0]`` closure could fire against an empty
    # list if anything in SyncLoop ever invoked the apply path during
    # construction; the explicit ``set_apply_fn`` setter rules that
    # footgun out at compile time.
    peer_watcher = PeerResolveWatcher()
    syncer = SyncLoop(
        cfg, token_ref, heartbeat, ha_poller=ha_poller, peer_watcher=peer_watcher
    )
    peer_watcher.set_apply_fn(
        lambda bundle, reload_kea=True: syncer._apply_bundle(bundle, reload_kea=reload_kea)
    )
    leases = LeaseWatcher(cfg, token_ref, heartbeat)
    metrics = MetricsPoller(cfg, token_ref)
    log_shipper = LogShipper(cfg, token_ref)

    # Passive DHCP fingerprinting is opt-in (Phase 2 device profiling).
    # Default off because:
    #   1. The container needs CAP_NET_RAW to bind the BPF socket, and
    #      we don't want to silently fail when the cap isn't granted.
    #   2. scapy is a heavyweight import — we don't want the cost on
    #      deployments that aren't using fingerprinting.
    # Operators flip DHCP_FINGERPRINT_ENABLED=1 in their compose env
    # to turn it on; the cap_add must be set in the compose override
    # too (see docs/deployment/DOCKER.md).
    fingerprint_enabled = os.environ.get("DHCP_FINGERPRINT_ENABLED", "0") == "1"
    fingerprint_shipper: DhcpFingerprintShipper | None = None
    if fingerprint_enabled:
        fingerprint_shipper = DhcpFingerprintShipper(cfg, token_ref)
        log.info("dhcp_fingerprint_enabled")

    # Active rogue-DHCP probe (issue #370) — opt-in for the same CAP_NET_RAW +
    # scapy reasons as fingerprinting. Broadcasts a DISCOVER on an interval and
    # ships observed OFFERs so the control plane can flag unknown responders.
    rogue_probe_enabled = os.environ.get("DHCP_ROGUE_PROBE_ENABLED", "0") == "1"
    rogue_probe: RogueProbeShipper | None = None
    if rogue_probe_enabled:
        rogue_probe = RogueProbeShipper(cfg, token_ref)
        log.info("dhcp_rogue_probe_enabled")

    threads = [
        threading.Thread(target=syncer.run, name="sync", daemon=True),
        threading.Thread(target=heartbeat.run, name="heartbeat", daemon=True),
        threading.Thread(target=leases.run, name="leases", daemon=True),
        threading.Thread(target=ha_poller.run, name="ha-status", daemon=True),
        threading.Thread(target=peer_watcher.run, name="peer-resolve", daemon=True),
        threading.Thread(target=metrics.run, name="metrics", daemon=True),
        threading.Thread(target=log_shipper.run, name="log-shipper", daemon=True),
    ]
    if fingerprint_shipper is not None:
        threads.append(
            threading.Thread(
                target=fingerprint_shipper.run,
                name="dhcp-fingerprint",
                daemon=True,
            )
        )
    if rogue_probe is not None:
        threads.append(
            threading.Thread(target=rogue_probe.run, name="rogue-probe", daemon=True)
        )
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
        if fingerprint_shipper is not None:
            fingerprint_shipper.stop()

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
