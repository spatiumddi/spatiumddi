"""Agent runtime configuration loaded from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    """Runtime configuration for the DHCP agent.

    Env vars:
        SPATIUM_API_URL            — control-plane base URL (required)
        SPATIUM_AGENT_KEY          — bootstrap pre-shared key (required)
        SPATIUM_SERVER_NAME        — hostname reported to the control plane
        CACHE_DIR                  — state directory (default /var/lib/spatium-dhcp-agent)
        KEA_CONFIG_PATH            — Kea dhcp4 config path (default /etc/kea/kea-dhcp4.conf)
        KEA_CONTROL_SOCKET         — Kea control unix socket (default /run/kea/kea4-ctrl-socket)
        KEA_LEASE_FILE             — Kea leases memfile (default /var/lib/kea/kea-leases4.csv)
        LONGPOLL_TIMEOUT           — seconds the control plane holds a long-poll (default 30)
        HEARTBEAT_INTERVAL         — seconds between heartbeats (default 30)
        AGENT_GROUP                — optional DHCP server group to join
        AGENT_ROLES                — comma-separated: primary,secondary,failover
        TLS_CA_PATH                — optional custom CA bundle
        SPATIUM_INSECURE_SKIP_TLS_VERIFY=1  — dev only
    """

    control_plane_url: str
    # Either ``agent_key`` (long hex PSK from SPATIUM_AGENT_KEY) OR
    # ``bootstrap_pairing_code`` (short-lived code via #169) must be
    # set. The resolved key is computed at bootstrap time — see
    # ``pairing.resolve_bootstrap_key``.
    agent_key: str
    bootstrap_pairing_code: str
    server_name: str
    state_dir: Path
    kea_config_path: Path
    kea_control_socket: Path
    kea_lease_file: Path
    group_name: str | None
    roles: list[str]
    tls_ca_path: str | None
    insecure_skip_tls_verify: bool
    heartbeat_interval: float = 30.0
    longpoll_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "AgentConfig":
        cp = os.environ.get("SPATIUM_API_URL", "").rstrip("/")
        if not cp:
            raise RuntimeError("SPATIUM_API_URL is required")
        key = os.environ.get("SPATIUM_AGENT_KEY", "")
        pairing_code = os.environ.get("BOOTSTRAP_PAIRING_CODE", "")
        # One-of validation. A cached resolved key on disk would also
        # suffice but we can't see it from here; the resolver raises
        # a clearer error if every source is exhausted.
        if not key and not pairing_code:
            raise RuntimeError(
                "One of SPATIUM_AGENT_KEY or BOOTSTRAP_PAIRING_CODE must be set."
            )
        roles_raw = os.environ.get("AGENT_ROLES", "primary")
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
        return cls(
            control_plane_url=cp,
            agent_key=key,
            bootstrap_pairing_code=pairing_code,
            server_name=(
                os.environ.get("SPATIUM_SERVER_NAME")
                or os.environ.get("AGENT_HOSTNAME")
                or os.uname().nodename
            ),
            state_dir=Path(os.environ.get("CACHE_DIR", "/var/lib/spatium-dhcp-agent")),
            kea_config_path=Path(
                os.environ.get("KEA_CONFIG_PATH", "/etc/kea/kea-dhcp4.conf")
            ),
            kea_control_socket=Path(
                os.environ.get("KEA_CONTROL_SOCKET", "/run/kea/kea4-ctrl-socket")
            ),
            kea_lease_file=Path(
                os.environ.get("KEA_LEASE_FILE", "/var/lib/kea/kea-leases4.csv")
            ),
            group_name=os.environ.get("AGENT_GROUP") or None,
            roles=roles,
            tls_ca_path=os.environ.get("TLS_CA_PATH") or None,
            insecure_skip_tls_verify=os.environ.get("SPATIUM_INSECURE_SKIP_TLS_VERIFY")
            == "1",
            heartbeat_interval=float(os.environ.get("HEARTBEAT_INTERVAL", "30")),
            longpoll_timeout=float(os.environ.get("LONGPOLL_TIMEOUT", "30")),
        )
