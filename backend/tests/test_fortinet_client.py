"""Unit tests for the FortiGate client's parse helpers + HTTP behaviour (#606).

The pure helpers (``classify_fortinet_address`` / ``_subnet_to_cidr`` /
``_normalise_mac`` / ``_first_ip``) are tested directly; the REST HTTP surface
is exercised through ``httpx.MockTransport`` so we validate the
``Authorization: Bearer`` header, the mandatory ``vdom`` query param,
``results`` unwrapping, and error mapping without a real firewall.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.fortinet.client import (
    FortinetClient,
    FortinetClientError,
    _first_ip,
    _normalise_mac,
    _subnet_to_cidr,
    classify_fortinet_address,
)

# ── classify_fortinet_address ────────────────────────────────────────


def test_classify_ipmask_host() -> None:
    assert classify_fortinet_address({"type": "ipmask", "subnet": "10.0.0.5 255.255.255.255"}) == (
        "host",
        "10.0.0.5/32",
    )


def test_classify_ipmask_network() -> None:
    assert classify_fortinet_address({"type": "ipmask", "subnet": "10.0.0.0 255.255.255.0"}) == (
        "network",
        "10.0.0.0/24",
    )


def test_classify_iprange() -> None:
    assert classify_fortinet_address(
        {"type": "iprange", "start-ip": "10.0.0.1", "end-ip": "10.0.0.9"}
    ) == ("range", "10.0.0.1-10.0.0.9")


def test_classify_fqdn() -> None:
    assert classify_fortinet_address({"type": "fqdn", "fqdn": "host.example.com"}) == (
        "fqdn",
        "host.example.com",
    )


def test_classify_geography_is_empty_host() -> None:
    assert classify_fortinet_address({"type": "geography", "country": "US"}) == ("host", "")


def test_classify_unknown_type_is_empty_host() -> None:
    assert classify_fortinet_address({"type": "wildcard"}) == ("host", "")


def test_classify_infers_ipmask_without_type() -> None:
    assert classify_fortinet_address({"subnet": "192.168.1.0 255.255.255.0"}) == (
        "network",
        "192.168.1.0/24",
    )


# ── _subnet_to_cidr ──────────────────────────────────────────────────


def test_subnet_to_cidr_space_form() -> None:
    assert _subnet_to_cidr("10.0.0.0 255.255.255.0") == "10.0.0.0/24"


def test_subnet_to_cidr_host() -> None:
    assert _subnet_to_cidr("10.0.0.5 255.255.255.255") == "10.0.0.5/32"


def test_subnet_to_cidr_passthrough() -> None:
    assert _subnet_to_cidr("10.0.0.0/24") == "10.0.0.0/24"


def test_subnet_to_cidr_garbage_is_none() -> None:
    assert _subnet_to_cidr("nonsense") is None
    assert _subnet_to_cidr("") is None


# ── _first_ip ────────────────────────────────────────────────────────


def test_first_ip_bare() -> None:
    assert _first_ip("1.2.3.4") == "1.2.3.4"


def test_first_ip_range_takes_first() -> None:
    assert _first_ip("1.2.3.4-1.2.3.5") == "1.2.3.4"


def test_first_ip_garbage_is_none() -> None:
    assert _first_ip("not-an-ip") is None
    assert _first_ip("") is None
    assert _first_ip(None) is None


# ── _normalise_mac ───────────────────────────────────────────────────


def test_normalise_mac() -> None:
    assert _normalise_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert _normalise_mac("(incomplete)") is None
    assert _normalise_mac("00:00:00:00:00:00") is None
    assert _normalise_mac(None) is None


# ── HTTP surface via MockTransport ────────────────────────────────────


def _client_with_handler(handler, **kwargs) -> FortinetClient:
    client = FortinetClient(
        host="fgt.example.test",
        port=443,
        api_token="TOKEN123",
        verify_tls=False,
        **kwargs,
    )
    client._client = httpx.AsyncClient(
        base_url="https://fgt.example.test:443",
        headers={
            "Authorization": "Bearer TOKEN123",
            "Accept": "application/json",
        },
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_bearer_header_vdom_param_and_system_info() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["vdom"] = request.url.params.get("vdom", "")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "version": "v7.4.3 build2573",
                "results": {
                    "hostname": "fgt-lab",
                    "model_name": "FortiGate-60F",
                    "serial": "FGT60F0001",
                },
            },
        )

    client = _client_with_handler(handler)
    try:
        info = await client.get_system_info()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["auth"] == "Bearer TOKEN123"
    assert seen["vdom"] == "root"
    assert seen["path"] == "/api/v2/monitor/system/status"
    assert info.version == "7.4.3"
    assert info.model == "FortiGate-60F"
    assert info.hostname == "fgt-lab"
    assert info.serial == "FGT60F0001"


@pytest.mark.asyncio
async def test_custom_vdom_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["vdom"] = request.url.params.get("vdom", "")
        return httpx.Response(200, json={"results": []})

    client = _client_with_handler(handler, vdom="prod")
    try:
        await client.list_address_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["vdom"] == "prod"


@pytest.mark.asyncio
async def test_address_objects_parsed_with_tags() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "web-host",
                        "type": "ipmask",
                        "subnet": "10.0.0.5 255.255.255.255",
                        "comment": "web",
                        "tagging": [{"tags": ["pci", "web"]}],
                    },
                    {"name": "net", "type": "ipmask", "subnet": "10.0.1.0 255.255.255.0"},
                    {
                        "name": "range1",
                        "type": "iprange",
                        "start-ip": "10.0.2.1",
                        "end-ip": "10.0.2.9",
                    },
                    {"name": "fq", "type": "fqdn", "fqdn": "host.example.com"},
                    {"name": "", "type": "ipmask", "subnet": "10.0.9.0 255.255.255.0"},
                ]
            },
        )

    client = _client_with_handler(handler)
    try:
        objs = await client.list_address_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["path"] == "/api/v2/cmdb/firewall/address"
    assert {o.name for o in objs} == {"web-host", "net", "range1", "fq"}
    web = next(o for o in objs if o.name == "web-host")
    assert web.kind == "host"
    assert web.value == "10.0.0.5/32"
    assert web.tags == ["pci", "web"]
    assert web.description == "web"
    net = next(o for o in objs if o.name == "net")
    assert (net.kind, net.value) == ("network", "10.0.1.0/24")
    rng = next(o for o in objs if o.name == "range1")
    assert (rng.kind, rng.value) == ("range", "10.0.2.1-10.0.2.9")
    fq = next(o for o in objs if o.name == "fq")
    assert (fq.kind, fq.value) == ("fqdn", "host.example.com")


@pytest.mark.asyncio
async def test_address_groups_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/cmdb/firewall/addrgrp"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "grp1",
                        "member": [{"name": "web-host"}, {"name": "net"}],
                        "comment": "servers",
                    }
                ]
            },
        )

    client = _client_with_handler(handler)
    try:
        grps = await client.list_address_groups()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(grps) == 1
    assert grps[0].kind == "group"
    assert grps[0].value == "web-host, net"
    assert grps[0].description == "servers"


@pytest.mark.asyncio
async def test_vip_nat_rule_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/cmdb/firewall/vip"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "web-vip",
                        "extip": "203.0.113.10",
                        "mappedip": [{"range": "10.0.0.5"}],
                        "comment": "inbound web",
                    }
                ]
            },
        )

    client = _client_with_handler(handler)
    try:
        rules = await client.list_nat_rules()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(rules) == 1
    r = rules[0]
    assert r.kind == "1to1"
    assert r.source == ""
    assert r.original_dst == "203.0.113.10"
    assert r.translated_dst == "10.0.0.5"
    assert r.translated_src is None
    assert r.description == "inbound web"


@pytest.mark.asyncio
async def test_vip_extip_range_takes_first_ip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "rng-vip",
                        "extip": "203.0.113.10-203.0.113.12",
                        "mappedip": "10.0.0.7",
                    }
                ]
            },
        )

    client = _client_with_handler(handler)
    try:
        rules = await client.list_nat_rules()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert rules[0].original_dst == "203.0.113.10"
    assert rules[0].translated_dst == "10.0.0.7"


@pytest.mark.asyncio
async def test_interfaces_parsed_and_skip_unset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/cmdb/system/interface"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "port1", "ip": "10.0.0.1 255.255.255.0", "zone": "lan"},
                    {"name": "port2", "ip": "0.0.0.0 0.0.0.0"},
                    {"name": "port3", "ip": "garbage"},
                ]
            },
        )

    client = _client_with_handler(handler)
    try:
        ifaces = await client.list_interfaces()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(ifaces) == 1
    assert ifaces[0].name == "port1"
    assert ifaces[0].cidr == "10.0.0.0/24"
    assert ifaces[0].address == "10.0.0.1"
    assert ifaces[0].zone == "lan"


@pytest.mark.asyncio
async def test_dhcp_leases_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/monitor/system/dhcp"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "ip": "10.0.0.50",
                        "mac": "AA-BB-CC-DD-EE-FF",
                        "hostname": "laptop",
                        "status": "leased",
                        "reserved": False,
                    },
                    {"ip": "", "mac": "11:22:33:44:55:66"},
                ]
            },
        )

    client = _client_with_handler(handler)
    try:
        leases = await client.list_dhcp_leases()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(leases) == 1
    lease = leases[0]
    assert lease.address == "10.0.0.50"
    assert lease.mac == "aa:bb:cc:dd:ee:ff"
    assert lease.hostname == "laptop"
    assert lease.state == "leased"


@pytest.mark.asyncio
async def test_401_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(FortinetClientError, match="401"):
            await client.list_address_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_403_raises_with_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(FortinetClientError) as excinfo:
            await client.list_address_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert excinfo.value.status_code == 403
