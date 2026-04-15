"""Bootstrap / registration loop for the DHCP agent.

Tries the cached JWT first; on 401 (or missing) falls back to PSK registration
against ``POST /api/v1/dhcp/agents/register``.
"""

from __future__ import annotations

import hashlib
import random
import time

import httpx
import structlog

from . import __version__
from .cache import load_or_create_agent_id, load_token, save_token
from .config import AgentConfig

log = structlog.get_logger(__name__)


def _fingerprint(agent_id: str) -> str:
    """SHA-256 fingerprint derived from the agent id."""
    return hashlib.sha256(agent_id.encode()).hexdigest()


def _client(cfg: AgentConfig) -> httpx.Client:
    verify: bool | str = True
    if cfg.insecure_skip_tls_verify:
        verify = False
    elif cfg.tls_ca_path:
        verify = cfg.tls_ca_path
    return httpx.Client(base_url=cfg.control_plane_url, verify=verify, timeout=30.0)


def register(cfg: AgentConfig) -> tuple[str, str, dict]:
    """Perform PSK bootstrap. Returns (agent_id, token, response_body)."""
    agent_id = load_or_create_agent_id(cfg.state_dir)
    body = {
        "hostname": cfg.server_name,
        "driver": "kea",
        "roles": cfg.roles,
        "group_name": cfg.group_name,
        "fingerprint": _fingerprint(agent_id),
        "agent_id": agent_id,
        "version": __version__,
    }
    backoff = 2.0
    while True:
        try:
            with _client(cfg) as c:
                resp = c.post(
                    "/api/v1/dhcp/agents/register",
                    json=body,
                    headers={"X-DHCP-Agent-Key": cfg.agent_key},
                )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("agent_token") or data.get("token")
                if not token:
                    log.error("register_response_missing_token", body=str(data)[:400])
                else:
                    save_token(cfg.state_dir, token)
                    log.info(
                        "dhcp_agent_registered",
                        server_id=data.get("server_id"),
                        pending_approval=data.get("pending_approval", False),
                    )
                    return agent_id, token, data
            else:
                log.warning("register_failed", status=resp.status_code, body=resp.text[:400])
        except httpx.HTTPError as e:
            log.warning("register_http_error", error=str(e))
        # jittered exponential backoff, cap 5 min
        sleep_for = min(backoff + random.uniform(0, 2), 300.0)
        time.sleep(sleep_for)
        backoff = min(backoff * 2, 300.0)


def ensure_token(cfg: AgentConfig) -> tuple[str, str]:
    """Load cached token or re-register. Returns (agent_id, token)."""
    agent_id = load_or_create_agent_id(cfg.state_dir)
    token = load_token(cfg.state_dir)
    if token:
        return agent_id, token
    agent_id, token, _ = register(cfg)
    return agent_id, token
