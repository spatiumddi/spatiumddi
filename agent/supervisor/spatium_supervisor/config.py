"""Environment-driven config for the supervisor.

Phase A1 only consumes the bare-minimum surface — enough to log a
useful identity line and idle. Wave A2 will extend this with identity
state-dir handling + control-plane URL semantics.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupervisorConfig:
    control_plane_url: str
    hostname: str
    state_dir: Path
    bootstrap_pairing_code: str
    heartbeat_interval_seconds: int

    @classmethod
    def from_env(cls) -> "SupervisorConfig":
        return cls(
            control_plane_url=os.environ.get("CONTROL_PLANE_URL", "").rstrip("/"),
            hostname=os.environ.get("APPLIANCE_HOSTNAME") or socket.gethostname(),
            state_dir=Path(os.environ.get("STATE_DIR", "/var/lib/spatium-supervisor")),
            bootstrap_pairing_code=os.environ.get("BOOTSTRAP_PAIRING_CODE", ""),
            heartbeat_interval_seconds=int(
                os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "60")
            ),
        )
