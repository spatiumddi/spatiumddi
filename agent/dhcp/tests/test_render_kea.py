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
