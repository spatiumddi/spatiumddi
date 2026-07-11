"""Unit tests for the Meraki client's parse helpers + HTTP behaviour (#606).

The pure helpers (``classify_meraki_object`` / ``_normalise_mac`` /
``_parse_next_link`` / ``_parse_retry_after``) are tested directly; the JSON
REST surface is exercised through ``httpx.MockTransport`` so we validate the
``Authorization: Bearer`` header, appliance-network filtering, VLAN + subnet
parsing (including the VLANs-disabled 400 → ``[]`` fallback), reservation
flattening, policy-object mapping, NAT-rule shaping, Link-header pagination,
the client-policy set (PUT), and error mapping without a real dashboard.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.meraki.client import (
    MerakiClient,
    MerakiClientError,
    _normalise_mac,
    _parse_next_link,
    _parse_retry_after,
    classify_meraki_object,
)

# ── classify_meraki_object ───────────────────────────────────────────


def test_classify_cidr_host_from_slash32() -> None:
    assert classify_meraki_object({"type": "cidr", "cidr": "10.0.0.5/32"}) == (
        "host",
        "10.0.0.5/32",
    )


def test_classify_cidr_network() -> None:
    assert classify_meraki_object({"type": "cidr", "cidr": "10.0.0.0/24"}) == (
        "network",
        "10.0.0.0/24",
    )


def test_classify_ip_and_mask_maps_like_cidr() -> None:
    assert classify_meraki_object({"type": "ipAndMask", "cidr": "192.168.1.0/24"}) == (
        "network",
        "192.168.1.0/24",
    )


def test_classify_ip_host() -> None:
    assert classify_meraki_object({"type": "ip", "ip": "10.0.0.9"}) == ("host", "10.0.0.9")


def test_classify_fqdn() -> None:
    assert classify_meraki_object({"type": "fqdn", "fqdn": "host.example.com"}) == (
        "fqdn",
        "host.example.com",
    )


def test_classify_empty() -> None:
    assert classify_meraki_object({"name": "x"}) == ("host", "")


def test_classify_typeless_falls_back_to_value_field() -> None:
    # Some firmwares omit `type` but still carry the value field.
    assert classify_meraki_object({"cidr": "10.1.0.0/16"}) == ("network", "10.1.0.0/16")


# ── _normalise_mac ───────────────────────────────────────────────────


def test_normalise_mac() -> None:
    assert _normalise_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert _normalise_mac("(incomplete)") is None
    assert _normalise_mac(None) is None


# ── _parse_next_link ─────────────────────────────────────────────────


def test_parse_next_link_quoted() -> None:
    header = '<https://api.meraki.com/api/v1/networks/N/clients?startingAfter=abc>; rel="next"'
    assert _parse_next_link(header) == (
        "https://api.meraki.com/api/v1/networks/N/clients?startingAfter=abc"
    )


def test_parse_next_link_absent() -> None:
    header = '<https://api.meraki.com/api/v1/x>; rel="first", <https://x/y>; rel="prev"'
    assert _parse_next_link(header) is None
    assert _parse_next_link(None) is None


# ── _parse_retry_after ───────────────────────────────────────────────


def test_parse_retry_after() -> None:
    assert _parse_retry_after("2") == 2.0
    assert _parse_retry_after(None) == 1.0
    assert _parse_retry_after("garbage") == 1.0


# ── HTTP surface via MockTransport ────────────────────────────────────


def _client_with_handler(handler, **kwargs) -> MerakiClient:
    client = MerakiClient(
        api_key="APIKEY",
        org_id="ORG1",
        verify_tls=False,
        **kwargs,
    )
    client._client = httpx.AsyncClient(
        base_url="https://api.meraki.com/api/v1",
        headers={"Authorization": "Bearer APIKEY", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_bearer_header_and_get_organization() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"id": "ORG1", "name": "Acme", "url": "https://dashboard/o/ORG1"}
        )

    client = _client_with_handler(handler)
    try:
        info = await client.get_organization()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["auth"] == "Bearer APIKEY"
    assert seen["path"].endswith("/organizations/ORG1")
    assert info.name == "Acme"
    assert info.url == "https://dashboard/o/ORG1"


@pytest.mark.asyncio
async def test_list_networks_filters_appliance_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "N1", "name": "HQ", "productTypes": ["appliance", "switch"]},
                {"id": "N2", "name": "CamOnly", "productTypes": ["camera"]},
                {"id": "N3", "name": "Branch", "productTypes": ["appliance"]},
            ],
        )

    client = _client_with_handler(handler)
    try:
        nets = await client.list_networks()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert {n.id for n in nets} == {"N1", "N3"}
    hq = next(n for n in nets if n.id == "N1")
    assert "appliance" in hq.product_types


@pytest.mark.asyncio
async def test_list_networks_honors_network_ids_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "N1", "name": "HQ", "productTypes": ["appliance"]},
                {"id": "N3", "name": "Branch", "productTypes": ["appliance"]},
            ],
        )

    client = _client_with_handler(handler)
    try:
        nets = await client.list_networks(network_ids=["N3"])
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert {n.id for n in nets} == {"N3"}


@pytest.mark.asyncio
async def test_list_vlans_parses_subnet_and_appliance_ip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "name": "Data",
                    "subnet": "192.168.1.0/24",
                    "applianceIp": "192.168.1.1",
                },
                {"id": 20, "name": "NoSubnet", "applianceIp": "0.0.0.0"},
            ],
        )

    client = _client_with_handler(handler)
    try:
        vlans = await client.list_vlans("N1", network_name="HQ")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(vlans) == 1  # the subnet-less vlan is skipped
    v = vlans[0]
    assert v.vlan_id == "10"
    assert v.cidr == "192.168.1.0/24"
    assert v.appliance_ip == "192.168.1.1"
    assert v.network_name == "HQ"


@pytest.mark.asyncio
async def test_list_vlans_handles_vlans_disabled_400() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": ["VLANs are not enabled for this network"]})

    client = _client_with_handler(handler)
    try:
        vlans = await client.list_vlans("N1")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert vlans == []


@pytest.mark.asyncio
async def test_list_vlans_other_400_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": ["Malformed request"]})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(MerakiClientError, match="400"):
            await client.list_vlans("N1")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_list_reservations_flattens_fixed_ip_assignments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "subnet": "192.168.1.0/24",
                    "fixedIpAssignments": {
                        "AA:BB:CC:DD:EE:01": {"ip": "192.168.1.50", "name": "printer"},
                        "AA:BB:CC:DD:EE:02": {"ip": "192.168.1.51", "name": "nas"},
                    },
                },
            ],
        )

    client = _client_with_handler(handler)
    try:
        res = await client.list_reservations("N1")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert {r.address for r in res} == {"192.168.1.50", "192.168.1.51"}
    printer = next(r for r in res if r.address == "192.168.1.50")
    assert printer.mac == "aa:bb:cc:dd:ee:01"
    assert printer.name == "printer"


@pytest.mark.asyncio
async def test_list_policy_objects_objects_and_groups() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/policyObjects/groups"):
            return httpx.Response(
                200,
                json=[{"name": "servers-grp", "objectIds": ["1", "2"]}],
            )
        return httpx.Response(
            200,
            json=[
                {"name": "web-host", "category": "network", "type": "cidr", "cidr": "10.0.0.5/32"},
                {"name": "lan-net", "category": "network", "type": "cidr", "cidr": "10.0.1.0/24"},
                {"name": "ext", "category": "network", "type": "fqdn", "fqdn": "a.example.com"},
            ],
        )

    client = _client_with_handler(handler)
    try:
        objs = await client.list_policy_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    by_name = {o.name: o for o in objs}
    assert by_name["web-host"].kind == "host"
    assert by_name["web-host"].tags == ["network"]
    assert by_name["lan-net"].kind == "network"
    assert by_name["ext"].kind == "fqdn"
    assert by_name["ext"].value == "a.example.com"
    grp = by_name["servers-grp"]
    assert grp.kind == "group"
    assert grp.value == "1, 2"
    assert grp.tags == ["group"]


@pytest.mark.asyncio
async def test_list_nat_rules_combines_1to1_and_port_forward() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oneToOneNatRules"):
            return httpx.Response(
                200,
                json={"rules": [{"name": "web", "publicIp": "203.0.113.5", "lanIp": "10.0.0.5"}]},
            )
        if request.url.path.endswith("/portForwardingRules"):
            return httpx.Response(
                200,
                json={"rules": [{"lanIp": "10.0.0.9", "publicPort": "8443"}]},
            )
        return httpx.Response(404, text="nope")

    client = _client_with_handler(handler)
    try:
        rules = await client.list_nat_rules("N1")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    kinds = {r.kind for r in rules}
    assert kinds == {"1to1", "port-forward"}
    one = next(r for r in rules if r.kind == "1to1")
    assert one.original_dst == "203.0.113.5"
    assert one.translated_dst == "10.0.0.5"
    pf = next(r for r in rules if r.kind == "port-forward")
    assert pf.original_dst is None
    assert pf.translated_dst == "10.0.0.9"
    assert pf.name == "pf-10.0.0.9:8443"


@pytest.mark.asyncio
async def test_401_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(MerakiClientError, match="401"):
            await client.get_organization()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_find_client_matches_by_mac() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "kc1", "mac": "AA:BB:CC:00:00:01", "ip": "10.0.0.5"},
                {"id": "kc2", "mac": "AA:BB:CC:00:00:02", "ip": "10.0.0.6"},
            ],
        )

    client = _client_with_handler(handler)
    try:
        cid = await client.find_client("N1", "aa-bb-cc-00-00-02")
        missing = await client.find_client("N1", "de:ad:be:ef:00:00")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert cid == "kc2"
    assert missing is None


@pytest.mark.asyncio
async def test_get_client_policy_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"mac": "aa:bb:cc:00:00:01", "devicePolicy": "Blocked"})

    client = _client_with_handler(handler)
    try:
        pol = await client.get_client_policy("N1", "kc1")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert pol.device_policy == "Blocked"
    assert pol.mac == "aa:bb:cc:00:00:01"


@pytest.mark.asyncio
async def test_set_client_policy_issues_put_with_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"mac": "x", "devicePolicy": "Blocked"})

    client = _client_with_handler(handler)
    try:
        await client.set_client_policy("N1", "kc1", "Blocked")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["method"] == "PUT"
    assert str(seen["path"]).endswith("/networks/N1/clients/kc1/policy")
    # Parse the body rather than string-match — httpx serialises JSON compactly
    # (no space after the colon), so a substring check on the pretty form fails.
    assert json.loads(str(seen["body"])) == {"devicePolicy": "Blocked"}


@pytest.mark.asyncio
async def test_list_clients_follows_link_pagination() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "startingAfter=page2" not in str(request.url):
            # First page — hand back one client + a rel="next" link.
            next_url = (
                "https://api.meraki.com/api/v1/networks/N1/clients?perPage=1000&startingAfter=page2"
            )
            return httpx.Response(
                200,
                json=[{"id": "c1", "mac": "AA:BB:CC:00:00:01", "ip": "10.0.0.5"}],
                headers={"Link": f'<{next_url}>; rel="next"'},
            )
        # Second page — no Link header, so pagination stops.
        return httpx.Response(
            200,
            json=[{"id": "c2", "mac": "AA:BB:CC:00:00:02", "ip": "10.0.0.6"}],
        )

    client = _client_with_handler(handler)
    try:
        rows = await client.list_clients("N1")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(calls) == 2
    assert {r.address for r in rows} == {"10.0.0.5", "10.0.0.6"}
    c1 = next(r for r in rows if r.address == "10.0.0.5")
    assert c1.mac == "aa:bb:cc:00:00:01"


@pytest.mark.asyncio
async def test_list_clients_skips_ipless() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "c1", "mac": "AA:BB:CC:00:00:01", "ip": "10.0.0.5", "description": "laptop"},
                {"id": "c2", "mac": "AA:BB:CC:00:00:02", "ip": None},
            ],
        )

    client = _client_with_handler(handler)
    try:
        rows = await client.list_clients("N1")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0].hostname == "laptop"
