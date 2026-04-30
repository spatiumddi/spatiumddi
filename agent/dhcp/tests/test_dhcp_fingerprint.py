"""Tests for the passive DHCP fingerprint sniffer's packet parser.

We don't actually run AsyncSniffer in the test env (no CAP_NET_RAW,
and no traffic to capture). Instead we build DHCP packets in-memory
via scapy's BOOTP/DHCP layers and assert the parser extracts the
fields we expect.

If scapy isn't installed (running unit tests without the agent's
optional dep), the entire module is skipped — the parser's only
real responsibility is layered on top of scapy's primitives.
"""

from __future__ import annotations

import pytest

scapy = pytest.importorskip("scapy")
from scapy.layers.dhcp import BOOTP, DHCP  # noqa: E402

from spatium_dhcp_agent.dhcp_fingerprint import (  # noqa: E402
    FingerprintObservation,
    parse_dhcp_packet,
)


def _make_discover(
    *,
    mac: bytes = b"\x52\x54\x00\xab\xcd\xef",
    param_req_list: list[int] | None = None,
    vendor_class: bytes | None = None,
    user_class: bytes | None = None,
    client_id: bytes | None = None,
) -> BOOTP:
    options: list = [("message-type", 1)]  # 1 = DISCOVER
    if param_req_list is not None:
        options.append(("param_req_list", param_req_list))
    if vendor_class is not None:
        options.append(("vendor_class_id", vendor_class))
    if user_class is not None:
        options.append(("user_class", user_class))
    if client_id is not None:
        options.append(("client_id", client_id))
    options.append("end")

    pkt = BOOTP(chaddr=mac + b"\x00" * 10) / DHCP(options=options)
    return pkt


def test_parse_discover_with_full_fingerprint() -> None:
    pkt = _make_discover(
        mac=b"\xaa\xbb\xcc\xdd\xee\xff",
        param_req_list=[1, 3, 6, 15, 31, 33, 43, 44, 46, 47, 119, 121, 249, 252],
        vendor_class=b"MSFT 5.0",
        user_class=b"iPXE",
        client_id=b"\x01\xaa\xbb\xcc\xdd\xee\xff",
    )
    obs = parse_dhcp_packet(pkt)
    assert obs is not None
    assert obs.mac_address == "aa:bb:cc:dd:ee:ff"
    assert obs.option_55 == "1,3,6,15,31,33,43,44,46,47,119,121,249,252"
    assert obs.option_60 == "MSFT 5.0"
    assert obs.option_77 == "iPXE"
    assert obs.client_id == "01aabbccddeeff"


def test_parse_request_message_type_3_accepted() -> None:
    pkt = BOOTP(chaddr=b"\x52\x54\x00\xab\xcd\xef" + b"\x00" * 10) / DHCP(
        options=[("message-type", 3), ("param_req_list", [1, 3]), "end"]
    )
    obs = parse_dhcp_packet(pkt)
    assert obs is not None
    assert obs.option_55 == "1,3"


def test_parse_offer_ignored() -> None:
    """Server-side message types are dropped — only DISCOVER + REQUEST count."""
    pkt = BOOTP(chaddr=b"\x52\x54\x00\xab\xcd\xef" + b"\x00" * 10) / DHCP(
        options=[("message-type", 2), ("param_req_list", [1, 3]), "end"]  # OFFER
    )
    assert parse_dhcp_packet(pkt) is None


def test_parse_packet_without_dhcp_layer_returns_none() -> None:
    # A bare BOOTP without DHCP options layer.
    pkt = BOOTP(chaddr=b"\x52\x54\x00\xab\xcd\xef" + b"\x00" * 10)
    assert parse_dhcp_packet(pkt) is None


def test_parse_empty_chaddr_returns_none() -> None:
    pkt = BOOTP(chaddr=b"") / DHCP(
        options=[("message-type", 1), ("param_req_list", [1]), "end"]
    )
    assert parse_dhcp_packet(pkt) is None


def test_parse_minimal_signature_with_only_vendor_class() -> None:
    """A REQUEST without param-req-list but with vendor-class is still useful."""
    pkt = BOOTP(chaddr=b"\xaa\xbb\xcc\x11\x22\x33" + b"\x00" * 10) / DHCP(
        options=[
            ("message-type", 3),
            ("vendor_class_id", b"udhcp 1.31.1"),
            "end",
        ]
    )
    obs = parse_dhcp_packet(pkt)
    assert obs is not None
    assert obs.option_55 is None
    assert obs.option_60 == "udhcp 1.31.1"


def test_parse_no_useful_signature_returns_none() -> None:
    """REQUEST with neither option-55 nor option-60 is dropped client-side."""
    pkt = BOOTP(chaddr=b"\xaa\xbb\xcc\xdd\xee\xff" + b"\x00" * 10) / DHCP(
        options=[("message-type", 1), "end"]
    )
    assert parse_dhcp_packet(pkt) is None


def test_observation_payload_round_trips() -> None:
    obs = FingerprintObservation(
        mac_address="aa:bb:cc:dd:ee:ff",
        option_55="1,3,6",
        option_60="MSFT 5.0",
    )
    payload = obs.to_payload()
    assert payload["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert payload["option_55"] == "1,3,6"
    assert payload["option_60"] == "MSFT 5.0"
    assert payload["option_77"] is None
    assert payload["client_id"] is None
