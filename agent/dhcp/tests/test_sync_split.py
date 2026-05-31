"""Sync-loop config-split tests (issue #330).

``kea-dhcp4 -t`` rejects a config file that carries a stray ``Dhcp6``
key (and ``kea-dhcp6 -t`` a stray ``Dhcp4``), so ``SyncLoop._apply_bundle``
must split the combined render into two files: ``Dhcp4`` only → the v4
config path, ``Dhcp6`` only → the v6 config path.

These drive ``_apply_bundle`` directly with ``reload_kea=False`` so no
live Kea / control socket is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spatium_dhcp_agent.config import AgentConfig
from spatium_dhcp_agent.sync import SyncLoop


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.daemon_status: dict[str, Any] = {}
        self.pending_acks: list[dict[str, Any]] = []


def _make_loop(cfg: AgentConfig) -> SyncLoop:
    return SyncLoop(cfg, token_ref=[""], heartbeat=_FakeHeartbeat())


def test_apply_splits_into_two_files(agent_cfg: AgentConfig) -> None:
    """A bundle with a v4 + v6 scope writes a Dhcp4-only file to the v4
    path and a Dhcp6-only file to the v6 path."""
    loop = _make_loop(agent_cfg)
    bundle = {
        "etag": "sha256:test",
        "scopes": [
            {
                "subnet_cidr": "192.0.2.0/24",
                "lease_time": 3600,
                "address_family": "ipv4",
                "pools": [
                    {"start_ip": "192.0.2.10", "end_ip": "192.0.2.50", "pool_type": "dynamic"}
                ],
                "statics": [],
            },
            {
                "subnet_cidr": "2001:db8::/64",
                "lease_time": 4800,
                "address_family": "ipv6",
                "v6_address_mode": "stateful",
                "pools": [
                    {
                        "start_ip": "2001:db8::1000",
                        "end_ip": "2001:db8::2000",
                        "pool_type": "dynamic",
                    }
                ],
                "statics": [],
            },
        ],
    }
    loop._apply_bundle(bundle, reload_kea=False)

    v4_doc = json.loads(Path(agent_cfg.kea_config_path).read_text())
    v6_doc = json.loads(Path(agent_cfg.kea_config_path_v6).read_text())

    # Each file carries exactly one top-level block — no stray cross-key.
    assert set(v4_doc.keys()) == {"Dhcp4"}
    assert set(v6_doc.keys()) == {"Dhcp6"}

    # v4 subnet lands only in the v4 file …
    assert [s["subnet"] for s in v4_doc["Dhcp4"]["subnet4"]] == ["192.0.2.0/24"]
    # … and the v6 subnet only in the v6 file.
    assert [s["subnet"] for s in v6_doc["Dhcp6"]["subnet6"]] == ["2001:db8::/64"]

    # The v6 daemon uses its own control socket + lease store.
    assert v6_doc["Dhcp6"]["control-socket"]["socket-name"] == str(
        agent_cfg.kea_control_socket_v6
    )
    assert v6_doc["Dhcp6"]["lease-database"]["name"].endswith("kea-leases6.csv")


def test_apply_writes_idle_v6_file_for_pure_v4_bundle(agent_cfg: AgentConfig) -> None:
    """Even a pure-v4 bundle writes a valid idle Dhcp6 file so kea-dhcp6
    (always-on) has a config to load."""
    loop = _make_loop(agent_cfg)
    bundle = {
        "etag": "sha256:test",
        "scopes": [
            {
                "subnet_cidr": "10.0.0.0/24",
                "lease_time": 3600,
                "pools": [],
                "statics": [],
            }
        ],
    }
    loop._apply_bundle(bundle, reload_kea=False)

    v6_doc = json.loads(Path(agent_cfg.kea_config_path_v6).read_text())
    assert set(v6_doc.keys()) == {"Dhcp6"}
    assert v6_doc["Dhcp6"]["subnet6"] == []
    assert v6_doc["Dhcp6"]["interfaces-config"]["interfaces"] == []
