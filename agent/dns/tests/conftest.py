"""Shared pytest fixtures for the DNS agent tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from spatium_dns_agent.config import AgentConfig


@pytest.fixture
def tmp_state(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path


@pytest.fixture
def agent_cfg(tmp_state: Path) -> AgentConfig:
    return AgentConfig(
        control_plane_url="http://localhost:8000",
        dns_agent_key="test-key",
        server_name="dns-test",
        driver="bind9",
        roles=["authoritative"],
        group_name="default",
        tls_ca_path=None,
        insecure_skip_tls_verify=True,
        state_dir=tmp_state,
    )
