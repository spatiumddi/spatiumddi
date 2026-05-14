"""CLI entrypoint — ``spatium-supervisor``.

Phase A1 main loop: configure logging, ensure the state-dir layout
exists, log an idle line, and sleep. Wave A2 will replace the sleep
loop with the real bootstrap → register → poll cycle.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import structlog

from .config import SupervisorConfig
from .log import configure_logging
from .state import ensure_layout


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    log = structlog.get_logger()

    cfg = SupervisorConfig.from_env()
    ensure_layout(cfg.state_dir)

    log.info(
        "supervisor.start",
        phase="A1-scaffolding",
        hostname=cfg.hostname,
        control_plane_url=cfg.control_plane_url or None,
        bootstrap_pairing_code_set=bool(cfg.bootstrap_pairing_code),
        state_dir=str(cfg.state_dir),
        heartbeat_interval_seconds=cfg.heartbeat_interval_seconds,
    )

    stop = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop
        log.info("supervisor.signal", signal=signum)
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Wave A1 does no real work. The heartbeat log line is just a
    # liveness signal for ``docker logs`` while the rest of the
    # waves land.
    while not stop:
        log.info("supervisor.idle", phase="A1-scaffolding")
        for _ in range(cfg.heartbeat_interval_seconds):
            if stop:
                break
            time.sleep(1)

    log.info("supervisor.stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
