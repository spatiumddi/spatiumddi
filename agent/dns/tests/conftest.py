"""Shared pytest fixtures for the DNS agent tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from spatium_dns_agent.config import AgentConfig


@pytest.fixture
def agent_cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        control_plane_url="http://localhost:8000",
        dns_agent_key="test-key",
        server_name="dns-test",
        driver="bind9",
        roles=["authoritative"],
        group_name="default",
        tls_ca_path=None,
        insecure_skip_tls_verify=True,
        state_dir=tmp_path,
    )
