"""DHCPv6 prefix delegation + DUID host reservations — Kea driver render (#368)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.drivers.dhcp.base import (
    ConfigBundle,
    PoolDef,
    ScopeDef,
    ServerOptionsDef,
    StaticAssignmentDef,
)
from app.drivers.dhcp.kea import KeaDriver


def _render(scope: ScopeDef) -> dict:
    bundle = ConfigBundle(
        server_id="s",
        server_name="n",
        driver="kea",
        roles=(),
        options=ServerOptionsDef(),
        scopes=(scope,),
        client_classes=(),
        generated_at=datetime.now(UTC),
    )
    return json.loads(KeaDriver().render_config(bundle))


def test_pd_pool_renders_in_subnet6() -> None:
    scope = ScopeDef(
        subnet_cidr="2001:db8::/48",
        address_family="ipv6",
        v6_address_mode="stateful",
        pools=(
            PoolDef(
                start_ip="2001:db8:1::",
                end_ip="2001:db8:1::",
                pool_type="pd",
                pd_prefix="2001:db8:1::/56",
                delegated_length=64,
                excluded_prefix="2001:db8:1:1::/64",
            ),
        ),
    )
    s6 = _render(scope)["Dhcp6"]["subnet6"][0]
    assert s6["pd-pools"] == [
        {
            "prefix": "2001:db8:1::",
            "prefix-len": 56,
            "delegated-len": 64,
            "excluded-prefix": "2001:db8:1:1::",
            "excluded-prefix-len": 64,
        }
    ]


def test_pd_pool_absent_for_v4_scope() -> None:
    scope = ScopeDef(
        subnet_cidr="10.0.0.0/24",
        address_family="ipv4",
        pools=(
            PoolDef(
                start_ip="10.0.0.0",
                end_ip="10.0.0.0",
                pool_type="pd",
                pd_prefix="2001:db8:1::/56",
                delegated_length=64,
            ),
        ),
    )
    s4 = _render(scope)["Dhcp4"]["subnet4"][0]
    assert "pd-pools" not in s4


def test_pd_pool_dropped_on_slaac_mode() -> None:
    scope = ScopeDef(
        subnet_cidr="2001:db8::/48",
        address_family="ipv6",
        v6_address_mode="slaac",
        pools=(
            PoolDef(
                start_ip="2001:db8:1::",
                end_ip="2001:db8:1::",
                pool_type="pd",
                pd_prefix="2001:db8:1::/56",
                delegated_length=64,
            ),
        ),
    )
    s6 = _render(scope)["Dhcp6"]["subnet6"][0]
    assert "pd-pools" not in s6


def test_duid_reservation_keys_on_duid() -> None:
    scope = ScopeDef(
        subnet_cidr="2001:db8::/64",
        address_family="ipv6",
        v6_address_mode="stateful",
        statics=(
            StaticAssignmentDef(
                ip_address="2001:db8::5",
                mac_address="aa:bb:cc:dd:ee:ff",
                duid="00:03:00:01:aa:bb:cc:dd:ee:ff",
                hostname="host",
            ),
        ),
    )
    d6 = _render(scope)["Dhcp6"]
    res = d6["subnet6"][0]["reservations"][0]
    assert res["duid"] == "00:03:00:01:aa:bb:cc:dd:ee:ff"
    assert "hw-address" not in res
    assert res["ip-addresses"] == ["2001:db8::5"]
    # The Dhcp6 block declares both identifier types.
    assert d6["host-reservation-identifiers"] == ["duid", "hw-address"]


def test_v6_reservation_falls_back_to_hw_address_without_duid() -> None:
    scope = ScopeDef(
        subnet_cidr="2001:db8::/64",
        address_family="ipv6",
        v6_address_mode="stateful",
        statics=(
            StaticAssignmentDef(
                ip_address="2001:db8::6",
                mac_address="aa:bb:cc:dd:ee:01",
                hostname="host2",
            ),
        ),
    )
    res = _render(scope)["Dhcp6"]["subnet6"][0]["reservations"][0]
    assert res["hw-address"] == "aa:bb:cc:dd:ee:01"
    assert "duid" not in res


def test_malformed_pd_pool_skipped() -> None:
    scope = ScopeDef(
        subnet_cidr="2001:db8::/48",
        address_family="ipv6",
        v6_address_mode="stateful",
        pools=(
            PoolDef(
                start_ip="2001:db8:1::",
                end_ip="2001:db8:1::",
                pool_type="pd",
                pd_prefix="not-a-prefix",
                delegated_length=64,
            ),
        ),
    )
    s6 = _render(scope)["Dhcp6"]["subnet6"][0]
    assert "pd-pools" not in s6
