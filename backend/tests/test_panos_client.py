"""Unit tests for the PAN-OS client's parse helpers + HTTP behaviour (#605).

The pure helpers (``classify_address_value`` / ``resolved_cidr_for`` /
``_normalise_mac`` / ``_xml_escape``) are tested directly; the REST + XML-API
HTTP surface is exercised through ``httpx.MockTransport`` so we validate the
``X-PAN-KEY`` header, REST ``result.entry`` unwrapping, op-command XML parsing,
keygen, User-ID register, and error mapping without a real firewall.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.services.panos.client import (
    PANOSClient,
    PANOSClientError,
    _normalise_mac,
    _xml_escape,
    classify_address_value,
    resolved_cidr_for,
)

# ── classify_address_value ───────────────────────────────────────────


def test_classify_host_from_slash32() -> None:
    assert classify_address_value({"ip-netmask": "10.0.0.5/32"}) == ("host", "10.0.0.5/32")


def test_classify_host_from_bare_ip() -> None:
    assert classify_address_value({"ip-netmask": "10.0.0.5"}) == ("host", "10.0.0.5")


def test_classify_network() -> None:
    assert classify_address_value({"ip-netmask": "10.0.0.0/24"}) == ("network", "10.0.0.0/24")


def test_classify_range() -> None:
    assert classify_address_value({"ip-range": "10.0.0.1-10.0.0.9"}) == (
        "range",
        "10.0.0.1-10.0.0.9",
    )


def test_classify_fqdn() -> None:
    assert classify_address_value({"fqdn": "host.example.com"}) == ("fqdn", "host.example.com")


def test_classify_wildcard_folds_to_network() -> None:
    kind, _ = classify_address_value({"ip-wildcard": "10.20.1.0/0.0.248.255"})
    assert kind == "network"


def test_classify_empty() -> None:
    assert classify_address_value({"@name": "x"}) == ("host", "")


# ── resolved_cidr_for ────────────────────────────────────────────────


def test_resolved_host() -> None:
    assert resolved_cidr_for("host", "10.0.0.5/32") == "10.0.0.5/32"
    assert resolved_cidr_for("host", "10.0.0.5") == "10.0.0.5/32"


def test_resolved_network() -> None:
    assert resolved_cidr_for("network", "10.0.0.0/24") == "10.0.0.0/24"


def test_resolved_range_takes_first_ip() -> None:
    assert resolved_cidr_for("range", "10.0.0.1-10.0.0.9") == "10.0.0.1/32"


def test_resolved_fqdn_is_none() -> None:
    assert resolved_cidr_for("fqdn", "host.example.com") is None


def test_resolved_group_is_none() -> None:
    assert resolved_cidr_for("group", "a, b, c") is None


def test_resolved_bad_value_is_none() -> None:
    assert resolved_cidr_for("host", "not-an-ip") is None
    assert resolved_cidr_for("range", "") is None


# ── _normalise_mac + _xml_escape ─────────────────────────────────────


def test_normalise_mac() -> None:
    assert _normalise_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert _normalise_mac("(incomplete)") is None
    assert _normalise_mac(None) is None


def test_xml_escape() -> None:
    assert _xml_escape("a&b<c>'\"") == "a&amp;b&lt;c&gt;&apos;&quot;"


# ── HTTP surface via MockTransport ────────────────────────────────────


def _client_with_handler(handler, **kwargs) -> PANOSClient:
    client = PANOSClient(
        host="pa.example.test",
        port=443,
        api_key="APIKEY",
        verify_tls=False,
        **kwargs,
    )
    client._client = httpx.AsyncClient(
        base_url="https://pa.example.test:443",
        headers={"X-PAN-KEY": "APIKEY", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    return client


_SYS_INFO_XML = (
    "<response status='success'><result><system>"
    "<hostname>pa-fw</hostname><model>PA-VM</model>"
    "<serial>0001</serial><sw-version>11.0.2</sw-version>"
    "</system></result></response>"
)


@pytest.mark.asyncio
async def test_x_pan_key_header_and_system_info() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-pan-key", "")
        seen["path"] = request.url.path
        return httpx.Response(200, text=_SYS_INFO_XML)

    client = _client_with_handler(handler)
    try:
        info = await client.get_system_info()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["key"] == "APIKEY"
    assert info.version == "11.0.2"
    assert info.model == "PA-VM"


@pytest.mark.asyncio
async def test_rest_address_objects_parsed_and_location_param() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["location"] = request.url.params.get("location", "")
        seen["vsys"] = request.url.params.get("vsys", "")
        return httpx.Response(
            200,
            json={
                "result": {
                    "entry": [
                        {
                            "@name": "web-host",
                            "ip-netmask": "10.0.0.5/32",
                            "description": "web",
                            "tag": {"member": ["pci", "web"]},
                        },
                        {"@name": "net", "ip-netmask": "10.0.1.0/24"},
                    ]
                }
            },
        )

    client = _client_with_handler(handler, vsys="vsys1")
    try:
        objs = await client.list_address_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["path"].endswith("/restapi/v10.1/Objects/Addresses")
    assert seen["location"] == "vsys"
    assert seen["vsys"] == "vsys1"
    assert {o.name for o in objs} == {"web-host", "net"}
    web = next(o for o in objs if o.name == "web-host")
    assert web.kind == "host"
    assert web.tags == ["pci", "web"]


@pytest.mark.asyncio
async def test_panorama_uses_device_group_location() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["location"] = request.url.params.get("location", "")
        seen["dg"] = request.url.params.get("device-group", "")
        return httpx.Response(200, json={"result": {"entry": []}})

    client = _client_with_handler(handler, is_panorama=True, device_group="DG1")
    try:
        await client.list_address_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["location"] == "device-group"
    assert seen["dg"] == "DG1"


@pytest.mark.asyncio
async def test_rest_401_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(PANOSClientError, match="401"):
            await client.list_address_objects()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_xml_error_envelope_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<response status='error'><msg>bad cmd</msg></response>")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(PANOSClientError, match="bad cmd"):
            await client.get_system_info()
    finally:
        await client._client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_register_ip_tag_builds_user_id_cmd() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["type"] = request.url.params.get("type", "")
        seen["cmd"] = request.url.params.get("cmd", "")
        seen["vsys"] = request.url.params.get("vsys", "")
        return httpx.Response(200, text="<response status='success'></response>")

    client = _client_with_handler(handler, vsys="vsys1")
    try:
        await client.register_ip_tag("10.0.0.9", "spatiumddi-quarantine")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert seen["type"] == "user-id"
    assert seen["vsys"] == "vsys1"
    assert "<register>" in seen["cmd"]
    assert "ip='10.0.0.9'" in seen["cmd"]
    assert "spatiumddi-quarantine" in seen["cmd"]


@pytest.mark.asyncio
async def test_list_registered_ips_parsed() -> None:
    xml = (
        "<response status='success'><result>"
        "<entry ip='10.0.0.9'><tag><member>spatiumddi-quarantine</member></tag></entry>"
        "<entry ip='10.0.0.10'><tag><member>spatiumddi-quarantine</member></tag></entry>"
        "</result></response>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    client = _client_with_handler(handler)
    try:
        regs = await client.list_registered_ips("spatiumddi-quarantine")
    finally:
        await client._client.aclose()  # type: ignore[union-attr]
    assert {r.ip for r in regs} == {"10.0.0.9", "10.0.0.10"}


@pytest.mark.asyncio
async def test_keygen_parses_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("type") == "keygen"
        return httpx.Response(
            200, text="<response status='success'><result><key>MINTED==</key></result></response>"
        )

    # keygen builds its own httpx client internally; drive it against
    # MockTransport by patching the constructor to inject the transport.
    orig = httpx.AsyncClient

    def _factory(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs.pop("verify", None)
        return orig(*args, transport=httpx.MockTransport(handler), **kwargs)

    with patch.object(httpx, "AsyncClient", _factory):
        key = await PANOSClient.keygen(
            host="pa.example.test", port=443, username="admin", password="pw", verify_tls=False
        )
    assert key == "MINTED=="
