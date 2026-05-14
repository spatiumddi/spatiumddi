"""Shared pytest fixtures for the DHCP agent tests."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from spatium_dhcp_agent.config import AgentConfig


@pytest.fixture
def tmp_state(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path


@pytest.fixture
def agent_cfg(tmp_state: Path) -> AgentConfig:
    return AgentConfig(
        control_plane_url="http://localhost:8000",
        agent_key="test-key",
        bootstrap_pairing_code="",
        server_name="dhcp-test",
        state_dir=tmp_state,
        kea_config_path=tmp_state / "kea-dhcp4.conf",
        kea_control_socket=tmp_state / "kea4-ctrl-socket",
        kea_lease_file=tmp_state / "leases.csv",
        group_name="default",
        roles=["primary"],
        tls_ca_path=None,
        insecure_skip_tls_verify=True,
    )
