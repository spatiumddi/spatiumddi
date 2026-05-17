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
    k8s_proxy_enabled: bool
    in_pod_firewall_enabled: bool

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
            # Phase 4 (#183) control-plane → kubeapi proxy. The
            # supervisor's loop long-polls a control-plane endpoint that
            # isn't shipped yet, so on current control planes every poll
            # returns 404 — ~12 log lines/min per appliance. Default to
            # disabled until the control-plane half lands; flip the env
            # to opt in for development against a branch that has it.
            k8s_proxy_enabled=os.environ.get("K8S_PROXY_ENABLED", "").lower() in (
                "1", "true", "yes", "on",
            ),
            # Phase 9 (#183) — in-pod nftables rendering is architecturally
            # broken: the supervisor runs in a k3s pod and writes drop-ins
            # to /etc/nftables.d/ inside the pod, then calls `nft -f
            # /etc/nftables.conf` which doesn't exist in the pod fs. Every
            # heartbeat logs `firewall.reload_failed`. Meanwhile the host's
            # /etc/nftables.conf already opens SSH / 53 / 67 / 80 / 443 and
            # the role pods all use hostNetwork=true, so they listen
            # directly on those ports without any per-role drop-in. The
            # supervisor's drop-in is duplicative noise. Default off; flip
            # to opt in only if you're actively developing the operator
            # CIDR-allowlist surface (Phase 6 kubeapi_expose_cidrs).
            in_pod_firewall_enabled=os.environ.get("IN_POD_FIREWALL_ENABLED", "").lower()
            in ("1", "true", "yes", "on"),
        )
