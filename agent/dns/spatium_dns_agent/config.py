"""Agent runtime configuration loaded from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    control_plane_url: str
    dns_agent_key: str
    server_name: str
    driver: str  # bind9 | powerdns
    roles: list[str]
    group_name: str | None
    tls_ca_path: str | None
    insecure_skip_tls_verify: bool
    state_dir: Path
    heartbeat_interval: float = 30.0
    longpoll_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "AgentConfig":
        cp = os.environ.get("CONTROL_PLANE_URL", "").rstrip("/")
        if not cp:
            raise RuntimeError("CONTROL_PLANE_URL is required")
        key = os.environ.get("DNS_AGENT_KEY", "")
        if not key:
            raise RuntimeError("DNS_AGENT_KEY is required")
        roles_raw = os.environ.get("AGENT_ROLES", "authoritative")
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
        return cls(
            control_plane_url=cp,
            dns_agent_key=key,
            server_name=os.environ.get("SERVER_NAME") or os.environ.get("AGENT_HOSTNAME") or os.uname().nodename,
            driver=os.environ.get("AGENT_DRIVER", "bind9"),
            roles=roles,
            group_name=os.environ.get("AGENT_GROUP") or None,
            tls_ca_path=os.environ.get("TLS_CA_PATH") or None,
            insecure_skip_tls_verify=os.environ.get("SPATIUM_INSECURE_SKIP_TLS_VERIFY") == "1",
            state_dir=Path(os.environ.get("AGENT_STATE_DIR", "/var/lib/spatium-dns-agent")),
        )
