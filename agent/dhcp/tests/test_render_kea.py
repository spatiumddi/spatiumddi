"""Golden-ish tests for the ConfigBundle → Kea JSON renderer.

Emphasis on non-negotiable: NTP servers MUST be emitted as DHCP option 42.
"""

from __future__ import annotations

import pytest

from spatium_dhcp_agent.render_kea import render


@pytest.fixture
def bundle() -> dict:
    return {
        "etag": "sha256:test",
        "schema_version": 1,
        "server": {"name": "dhcp1", "interfaces": ["eth0"]},
        "global_options": {
            "dns_servers": ["1.1.1.1", "9.9.9.9"],
            "ntp_servers": ["192.0.2.123", "192.0.2.124"],
            "domain_name": "example.com",
            "lease_time": 7200,
        },
        "subnets": [
            {
                "id": 1,
                "subnet": "192.0.2.0/24",
                "pools": [{"pool": "192.0.2.100 - 192.0.2.200"}],
                "options": {
                    "routers": ["192.0.2.1"],
                    "ntp_servers": ["192.0.2.5"],
                },
                "reservations": [
                    {
                        "hw_address": "aa:bb:cc:dd:ee:ff",
                        "ip_address": "192.0.2.50",
                        "hostname": "printer1",
                    }
                ],
                "valid_lifetime": 3600,
            }
        ],
        "client_classes": [
            {"name": "voip", "test": "substring(option[60].hex,0,12) == 'Cisco-Phone'"}
        ],
    }


def test_render_shape(bundle: dict) -> None:
    out = render(bundle)
    assert "Dhcp4" in out
    d = out["Dhcp4"]
    assert d["valid-lifetime"] == 7200
    assert d["subnet4"][0]["subnet"] == "192.0.2.0/24"
    assert d["subnet4"][0]["pools"] == [{"pool": "192.0.2.100 - 192.0.2.200"}]


def test_global_ntp_option_42_emitted(bundle: dict) -> None:
    """Non-negotiable: NTP via DHCP option 42 must be present."""
    out = render(bundle)
    opts = out["Dhcp4"]["option-data"]
    ntp = [o for o in opts if o["name"] == "ntp-servers"]
    assert len(ntp) == 1
    assert ntp[0]["code"] == 42
    assert "192.0.2.123" in ntp[0]["data"]
    assert "192.0.2.124" in ntp[0]["data"]


def test_per_subnet_ntp_option_42_emitted(bundle: dict) -> None:
    out = render(bundle)
    sub_opts = out["Dhcp4"]["subnet4"][0]["option-data"]
    ntp = [o for o in sub_opts if o["name"] == "ntp-servers"]
    assert len(ntp) == 1
    assert ntp[0]["code"] == 42
    assert ntp[0]["data"] == "192.0.2.5"


def test_reservations_rendered(bundle: dict) -> None:
    out = render(bundle)
    resv = out["Dhcp4"]["subnet4"][0]["reservations"]
    assert resv[0]["hw-address"] == "aa:bb:cc:dd:ee:ff"
    assert resv[0]["ip-address"] == "192.0.2.50"
    assert resv[0]["hostname"] == "printer1"


def test_client_classes_rendered(bundle: dict) -> None:
    out = render(bundle)
    cc = out["Dhcp4"]["client-classes"]
    assert cc[0]["name"] == "voip"
    assert "Cisco-Phone" in cc[0]["test"]


def test_control_socket_and_lease_paths_overrideable() -> None:
    out = render(
        {"server": {}, "subnets": []},
        control_socket="/tmp/sock",
        lease_file="/tmp/leases.csv",
    )
    assert out["Dhcp4"]["control-socket"]["socket-name"] == "/tmp/sock"
    assert out["Dhcp4"]["lease-database"]["name"] == "/tmp/leases.csv"


def test_lease_cmds_hook_enabled() -> None:
    out = render({"server": {}, "subnets": []})
    libs = [h["library"] for h in out["Dhcp4"]["hooks-libraries"]]
    assert any("libdhcp_lease_cmds.so" in lib for lib in libs)


# ── Canonical wire-shape (``scopes``) — the real payload agents receive ──


@pytest.fixture
def wire_bundle() -> dict:
    """Shape matches ``backend/app/api/v1/dhcp/agents.py`` serialization."""
    return {
        "etag": "sha256:test",
        "server_name": "dhcp-kea",
        "driver": "kea",
        "roles": [],
        "scopes": [
            {
                "subnet_cidr": "10.20.0.0/21",
                "lease_time": 3600,
                "options": {
                    "routers": ["10.20.0.1"],
                    "dns_servers": ["10.20.0.2"],
                },
                "pools": [
                    {
                        "start_ip": "10.20.0.10",
                        "end_ip": "10.20.7.254",
                        "pool_type": "dynamic",
                    },
                    {
                        "start_ip": "10.20.0.100",
                        "end_ip": "10.20.0.110",
                        "pool_type": "excluded",
                    },
                ],
                "statics": [
                    {
                        "ip_address": "10.20.0.50",
                        "mac_address": "aa:bb:cc:dd:ee:ff",
                        "hostname": "printer1",
                    }
                ],
                "ddns_enabled": False,
            }
        ],
        "client_classes": [
            {
                "name": "voip",
                "match_expression": "substring(option[60].hex,0,12) == 'Cisco-Phone'",
                "options": {},
            }
        ],
    }


def test_render_wire_shape_subnet_and_dynamic_pool(wire_bundle: dict) -> None:
    out = render(wire_bundle)
    subs = out["Dhcp4"]["subnet4"]
    assert len(subs) == 1
    assert subs[0]["subnet"] == "10.20.0.0/21"
    # id is a stable positive int derived from the CIDR
    assert isinstance(subs[0]["id"], int) and subs[0]["id"] > 0
    # Only the dynamic pool makes it through — excluded pools are
    # IPAM-level and must not become Kea lease pools.
    assert subs[0]["pools"] == [{"pool": "10.20.0.10 - 10.20.7.254"}]


def test_render_wire_shape_reservation_from_statics(wire_bundle: dict) -> None:
    out = render(wire_bundle)
    resv = out["Dhcp4"]["subnet4"][0]["reservations"]
    assert resv[0]["hw-address"] == "aa:bb:cc:dd:ee:ff"
    assert resv[0]["ip-address"] == "10.20.0.50"
    assert resv[0]["hostname"] == "printer1"


def test_render_wire_shape_client_class_match_expression(wire_bundle: dict) -> None:
    out = render(wire_bundle)
    cc = out["Dhcp4"]["client-classes"]
    assert cc[0]["name"] == "voip"
    assert "Cisco-Phone" in cc[0]["test"]


def test_render_wire_shape_subnet_id_is_stable() -> None:
    """Kea keys leases off subnet-id — the same CIDR must always hash
    to the same id across renders, otherwise a config reload would
    orphan every active lease."""
    bundle = {
        "scopes": [
            {
                "subnet_cidr": "192.0.2.0/24",
                "lease_time": 3600,
                "pools": [],
                "statics": [],
            }
        ]
    }
    out1 = render(bundle)
    out2 = render(bundle)
    assert out1["Dhcp4"]["subnet4"][0]["id"] == out2["Dhcp4"]["subnet4"][0]["id"]


# ── MAC blocklist → Kea DROP class ─────────────────────────────────


def test_mac_blocks_render_as_drop_class() -> None:
    """The wire carries ``mac_blocks``; the renderer must fold them into
    the reserved ``DROP`` client class as an OR-ed ``hexstring(pkt4.mac,
    ':')`` expression. That's the Kea-recommended way to drop a packet
    pre-allocation."""
    bundle = {
        "server": {"name": "t", "interfaces": ["eth0"]},
        "subnets": [{"id": 1, "subnet": "192.0.2.0/24", "pools": []}],
        "mac_blocks": [
            {"mac_address": "aa:bb:cc:dd:ee:ff", "reason": "rogue", "description": ""},
            {"mac_address": "11:22:33:44:55:66", "reason": "policy", "description": ""},
        ],
    }
    out = render(bundle)
    classes = out["Dhcp4"]["client-classes"]
    drop = [c for c in classes if c["name"] == "DROP"]
    assert len(drop) == 1, f"expected one DROP class, got {classes}"
    test_expr = drop[0]["test"]
    assert "aa:bb:cc:dd:ee:ff" in test_expr
    assert "11:22:33:44:55:66" in test_expr
    assert " or " in test_expr
    assert "hexstring(pkt4.mac, ':')" in test_expr


def test_no_drop_class_when_blocklist_empty() -> None:
    """An empty blocklist must not leak an empty DROP expression into
    Kea — that'd parse as "always false" but noise the config."""
    bundle = {
        "server": {"name": "t", "interfaces": ["eth0"]},
        "subnets": [{"id": 1, "subnet": "192.0.2.0/24", "pools": []}],
        "mac_blocks": [],
    }
    out = render(bundle)
    classes = out["Dhcp4"].get("client-classes", [])
    assert not any(c["name"] == "DROP" for c in classes)


def test_mac_blocklist_skips_invalid_entries() -> None:
    """A single malformed MAC shouldn't break rendering for the rest."""
    bundle = {
        "server": {"name": "t", "interfaces": ["eth0"]},
        "subnets": [{"id": 1, "subnet": "192.0.2.0/24", "pools": []}],
        "mac_blocks": [
            {"mac_address": "totally not a mac", "reason": "other", "description": ""},
            {"mac_address": "aa:bb:cc:dd:ee:ff", "reason": "rogue", "description": ""},
        ],
    }
    out = render(bundle)
    drop = next(c for c in out["Dhcp4"]["client-classes"] if c["name"] == "DROP")
    assert "aa:bb:cc:dd:ee:ff" in drop["test"]
    assert "totally" not in drop["test"]


def test_user_drop_class_not_clobbered() -> None:
    """If an operator manually defined a ``DROP`` client class, the
    renderer must not overwrite it with the blocklist-generated one."""
    bundle = {
        "server": {"name": "t", "interfaces": ["eth0"]},
        "subnets": [{"id": 1, "subnet": "192.0.2.0/24", "pools": []}],
        "client_classes": [{"name": "DROP", "match_expression": "option[60].hex == 'bad'"}],
        "mac_blocks": [
            {"mac_address": "aa:bb:cc:dd:ee:ff", "reason": "rogue", "description": ""}
        ],
    }
    out = render(bundle)
    drops = [c for c in out["Dhcp4"]["client-classes"] if c["name"] == "DROP"]
    assert len(drops) == 1
    # The user's test expression wins; the blocklist is silently skipped.
    assert "option[60]" in drops[0]["test"]
    assert "aa:bb:cc:dd:ee:ff" not in drops[0]["test"]
