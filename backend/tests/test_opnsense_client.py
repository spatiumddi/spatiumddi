"""Unit tests for the OPNsense client's parse helpers + HTTP behaviour.

The pure-function helpers (``_unwrap_rows`` / ``_network_cidr`` /
``_normalise_mac``) are tested directly; the HTTP surface is exercised
through ``httpx.MockTransport`` so we validate auth, the ``rows[]``
envelope handling, the ISC ``getReservation`` tree shape, and error
status mapping without a real OPNsense box.

Assumed JSON shapes (OPNsense API, documented because we can't hit a
real box):

* ``searchLease``  → ``{"rows": [{"address","mac","hostname","status"}], "total": N}``
* ``getReservation`` (newer) → ``{"rows": [{"ipaddr","mac","hostname","descr"}]}``
* ``getReservation`` (ISC tree) → ``{"dhcpd": {"lan": {"staticmap": {"<uuid>": {...}}}}}``
* ``getInterfaceConfig`` → ``{"lan": {"device","descr","ipaddr","subnet"}}``
* ``vlan_settings/get`` → ``{"vlan": {"vlan": {"<uuid>": {"vlanif","if","tag","descr"}}}}``
* ``getArp`` → ``{"rows": [{"ip","mac","hostname","intf"}]}`` (or a bare list)
"""

from __future__ import annotations

import httpx
import pytest

from app.services.opnsense.client import (
    OPNsenseClient,
    OPNsenseClientError,
    _network_cidr,
    _normalise_mac,
    _unwrap_rows,
)

# ── _unwrap_rows ──────────────────────────────────────────────────────


def test_unwrap_rows_envelope() -> None:
    assert _unwrap_rows({"rows": [{"a": 1}, {"b": 2}], "total": 2}) == [{"a": 1}, {"b": 2}]


def test_unwrap_rows_bare_list() -> None:
    assert _unwrap_rows([{"a": 1}]) == [{"a": 1}]


def test_unwrap_rows_keyed_dict() -> None:
    out = _unwrap_rows({"0": {"a": 1}, "1": {"b": 2}})
    assert {"a": 1} in out and {"b": 2} in out


def test_unwrap_rows_drops_non_dict_members() -> None:
    assert _unwrap_rows({"rows": [{"a": 1}, "junk", 7]}) == [{"a": 1}]


def test_unwrap_rows_garbage_returns_empty() -> None:
    assert _unwrap_rows(None) == []
    assert _unwrap_rows("nope") == []


# ── _network_cidr ─────────────────────────────────────────────────────


def test_network_cidr_basic() -> None:
    assert _network_cidr("10.0.0.1", 24) == "10.0.0.0/24"


def test_network_cidr_string_prefix() -> None:
    assert _network_cidr("192.168.5.1", "25") == "192.168.5.0/25"


def test_network_cidr_bad_prefix_returns_none() -> None:
    assert _network_cidr("10.0.0.1", "dhcp") is None
    assert _network_cidr("10.0.0.1", None) is None


def test_network_cidr_bad_address_returns_none() -> None:
    assert _network_cidr("not-an-ip", 24) is None


# ── _normalise_mac ────────────────────────────────────────────────────


def test_normalise_mac_lowercases_and_dashes() -> None:
    assert _normalise_mac("BC-24-11-E8-4A-3F") == "bc:24:11:e8:4a:3f"


def test_normalise_mac_drops_placeholders() -> None:
    assert _normalise_mac("(incomplete)") is None
    assert _normalise_mac("00:00:00:00:00:00") is None
    assert _normalise_mac("") is None
    assert _normalise_mac(None) is None


# ── HTTP surface via MockTransport ────────────────────────────────────


def _client_with_handler(handler) -> OPNsenseClient:
    client = OPNsenseClient(
        host="fw.example.test",
        port=443,
        api_key="KEY",
        api_secret="SECRET",
        verify_tls=False,
    )
    # Inject a MockTransport-backed httpx client, bypassing __aenter__'s
    # TLS context construction. The auth + base_url still apply.
    client._client = httpx.AsyncClient(
        base_url="https://fw.example.test:443",
        auth=("KEY", "SECRET"),
        headers={"Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_basic_auth_header_is_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"product_version": "OPNsense 24.7"})

    client = _client_with_handler(handler)
    try:
        fw = await client.get_firmware()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert fw.version == "OPNsense 24.7"
    # KEY:SECRET base64 == S0VZOlNFQ1JFVA==
    assert seen["auth"] == "Basic S0VZOlNFQ1JFVA=="


@pytest.mark.asyncio
async def test_401_raises_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(OPNsenseClientError, match="401"):
            await client.get_firmware()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_leases_parsed_from_rows_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "address": "10.0.0.50",
                        "mac": "BC:24:11:E8:4A:3F",
                        "hostname": "laptop",
                        "status": "active",
                    },
                    {"address": "10.0.0.51", "hwaddr": "(incomplete)", "state": "expired"},
                ],
                "total": 2,
            },
        )

    client = _client_with_handler(handler)
    try:
        leases = await client.list_leases()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(leases) == 2
    assert leases[0].address == "10.0.0.50"
    assert leases[0].mac == "bc:24:11:e8:4a:3f"
    assert leases[0].hostname == "laptop"
    assert leases[0].state == "active"
    # The placeholder MAC is normalised away.
    assert leases[1].mac is None
    assert leases[1].state == "expired"


@pytest.mark.asyncio
async def test_reservations_parsed_from_isc_tree() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "dhcpd": {
                    "lan": {
                        "staticmap": {
                            "uuid-1": {
                                "ipaddr": "10.0.0.10",
                                "mac": "AA:BB:CC:DD:EE:FF",
                                "hostname": "printer",
                                "descr": "office printer",
                            }
                        }
                    }
                }
            },
        )

    client = _client_with_handler(handler)
    try:
        res = await client.list_reservations()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(res) == 1
    assert res[0].address == "10.0.0.10"
    assert res[0].mac == "aa:bb:cc:dd:ee:ff"
    assert res[0].hostname == "printer"
    assert res[0].description == "office printer"


@pytest.mark.asyncio
async def test_interfaces_skip_dynamic_and_compute_cidr() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "lan": {
                    "device": "igb1",
                    "descr": "LAN",
                    "ipaddr": "10.0.0.1",
                    "subnet": "24",
                },
                "wan": {
                    "device": "igb0",
                    "descr": "WAN",
                    "ipaddr": "dhcp",
                    "subnet": "",
                },
                "opt1": {
                    "device": "vlan0.20",
                    "descr": "IoT",
                    "ipaddr": "192.168.20.1",
                    "subnet": "24",
                },
            },
        )

    client = _client_with_handler(handler)
    try:
        ifaces = await client.list_interfaces()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    cidrs = {i.cidr for i in ifaces}
    assert "10.0.0.0/24" in cidrs
    assert "192.168.20.0/24" in cidrs
    # WAN with ipaddr=dhcp contributes nothing.
    assert len(ifaces) == 2


@pytest.mark.asyncio
async def test_vlans_parsed_from_nested_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "vlan": {
                    "vlan": {
                        "uuid-1": {
                            "vlanif": "vlan0.20",
                            "if": "igb1",
                            "tag": "20",
                            "descr": "IoT",
                        }
                    }
                }
            },
        )

    client = _client_with_handler(handler)
    try:
        vlans = await client.list_vlans()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(vlans) == 1
    assert vlans[0].device == "vlan0.20"
    assert vlans[0].parent == "igb1"
    assert vlans[0].tag == 20
    assert vlans[0].description == "IoT"


@pytest.mark.asyncio
async def test_arp_parsed_from_bare_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"ip": "10.0.0.50", "mac": "bc:24:11:e8:4a:3f", "intf": "igb1"},
                {"ip": "10.0.0.99", "mac": "(incomplete)", "intf": "igb1"},
            ],
        )

    client = _client_with_handler(handler)
    try:
        arp = await client.list_arp()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert len(arp) == 2
    assert arp[0].address == "10.0.0.50"
    assert arp[0].mac == "bc:24:11:e8:4a:3f"
    assert arp[1].mac is None


@pytest.mark.asyncio
async def test_leases_404_returns_empty_not_error() -> None:
    """DHCP service not enabled → 404 on searchLease should degrade to
    an empty list, not fail the whole reconcile."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = _client_with_handler(handler)
    try:
        leases = await client.list_leases()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert leases == []
