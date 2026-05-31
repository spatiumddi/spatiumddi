"""Supervisor lldpcli json0 neighbour parser (#347).

``read_lldp_neighbours`` shells to ``lldpcli`` (validated on real appliances);
this covers the pure parsing of its json0 output — the fiddly bit — against a
representative document, plus the defensive skip of incomplete entries.
"""

from __future__ import annotations

from spatium_supervisor.appliance_state import _parse_lldp_neighbours

# Representative ``lldpcli show neighbors -f json0`` document — json0 wraps
# every node in arrays and nests leaf values as ``{"value": ...}``.
_JSON0 = {
    "lldp": [
        {
            "interface": [
                {
                    "name": "eth0",
                    "chassis": [
                        {
                            "id": [{"type": "mac", "value": "00:11:22:33:44:55"}],
                            "name": [{"value": "switch1"}],
                            "descr": [{"value": "Cisco IOS"}],
                            "mgmt-ip": [{"value": "10.0.0.1"}],
                            "capability": [
                                {"type": "Bridge", "enabled": True},
                                {"type": "Router", "enabled": False},
                                {"type": "Wlan", "enabled": True},
                            ],
                        }
                    ],
                    "port": [
                        {
                            "id": [{"type": "ifname", "value": "Gi0/1"}],
                            "descr": [{"value": "uplink"}],
                        }
                    ],
                },
                {
                    # Incomplete entry (no port id) → skipped.
                    "name": "eth1",
                    "chassis": [{"id": [{"type": "mac", "value": "aa:bb:cc:dd:ee:ff"}]}],
                    "port": [{"descr": [{"value": "nope"}]}],
                },
            ]
        }
    ]
}


def test_parse_json0_neighbour() -> None:
    out = _parse_lldp_neighbours(_JSON0)
    assert len(out) == 1  # the incomplete eth1 entry was skipped
    n = out[0]
    assert n["local_iface"] == "eth0"
    assert n["remote_chassis_id"] == "00:11:22:33:44:55"
    assert n["remote_port_id"] == "Gi0/1"
    assert n["remote_port_descr"] == "uplink"
    assert n["remote_sys_name"] == "switch1"
    assert n["remote_sys_descr"] == "Cisco IOS"
    assert n["remote_mgmt_ip"] == "10.0.0.1"
    # Only enabled capabilities, in document order.
    assert n["remote_caps"] == "Bridge,Wlan"


def test_parse_handles_name_keyed_chassis() -> None:
    # Some lldpd builds key chassis by sys-name with no "id" sibling.
    doc = {
        "lldp": [
            {
                "interface": [
                    {
                        "name": "eno1",
                        "chassis": {"sw2": {"id": [{"type": "mac", "value": "de:ad:be:ef:00:01"}]}},
                        "port": {"id": [{"type": "local", "value": "7"}]},
                    }
                ]
            }
        ]
    }
    out = _parse_lldp_neighbours(doc)
    assert len(out) == 1
    assert out[0]["remote_chassis_id"] == "de:ad:be:ef:00:01"
    assert out[0]["remote_port_id"] == "7"
    # The chassis KEY is the sys-name in this shape — must be preserved (#349).
    assert out[0]["remote_sys_name"] == "sw2"


def test_parse_empty_and_garbage() -> None:
    assert _parse_lldp_neighbours({}) == []
    assert _parse_lldp_neighbours({"lldp": []}) == []
    assert _parse_lldp_neighbours("not a dict") == []
    assert _parse_lldp_neighbours({"lldp": [{"interface": []}]}) == []
