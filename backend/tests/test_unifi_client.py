"""Unit tests for the UniFi client's pure-function parsers.

The HTTP / transport layer is exercised in
``test_unifi_reconcile`` via a fake client; these tests cover the
shape-conversion helpers that hydrate dataclasses from raw UniFi
wire payloads.
"""

from __future__ import annotations

from app.services.unifi.client import (
    _ip_subnet_to_cidr,
    _ip_subnet_to_gateway,
    _is_ipam_relevant_purpose,
    _normalise_mac,
    _parse_client_row,
)

# ── _normalise_mac ────────────────────────────────────────────────────


def test_normalise_mac_canonical_form() -> None:
    assert _normalise_mac("BC:24:11:E8:4A:3F") == "bc:24:11:e8:4a:3f"


def test_normalise_mac_with_dashes() -> None:
    # UniFi emits colon-separated MACs; dash form is a courtesy for
    # operator-pasted reservations that came in via copy/paste from
    # vendor docs.
    assert _normalise_mac("BC-24-11-E8-4A-3F") == "bc:24:11:e8:4a:3f"


def test_normalise_mac_empty_or_garbage() -> None:
    assert _normalise_mac("") == ""
    assert _normalise_mac("not-a-mac") == ""
    assert _normalise_mac("BC:24:11") == ""


# ── _ip_subnet_to_cidr / _gateway ─────────────────────────────────────


def test_ip_subnet_to_cidr_strips_host_bits() -> None:
    # UniFi's ``ip_subnet`` is gateway/prefix; convert to network form.
    assert _ip_subnet_to_cidr("10.0.0.1/24") == "10.0.0.0/24"
    assert _ip_subnet_to_cidr("192.168.1.1/16") == "192.168.0.0/16"
    assert _ip_subnet_to_cidr("172.16.5.5/22") == "172.16.4.0/22"


def test_ip_subnet_to_cidr_handles_v6() -> None:
    assert _ip_subnet_to_cidr("2001:db8::1/64") == "2001:db8::/64"


def test_ip_subnet_to_cidr_returns_none_on_garbage() -> None:
    assert _ip_subnet_to_cidr(None) is None
    assert _ip_subnet_to_cidr("") is None
    assert _ip_subnet_to_cidr("not-an-ip") is None


def test_ip_subnet_to_gateway_returns_host_part() -> None:
    assert _ip_subnet_to_gateway("10.0.0.1/24") == "10.0.0.1"
    assert _ip_subnet_to_gateway("192.168.1.254/24") == "192.168.1.254"


def test_ip_subnet_to_gateway_returns_none_on_garbage() -> None:
    assert _ip_subnet_to_gateway(None) is None
    assert _ip_subnet_to_gateway("") is None


# ── _is_ipam_relevant_purpose ─────────────────────────────────────────


def test_corporate_and_guest_are_relevant() -> None:
    assert _is_ipam_relevant_purpose("corporate") is True
    assert _is_ipam_relevant_purpose("guest") is True


def test_remote_user_vpn_is_relevant() -> None:
    # Remote-user VPN networks have a real CIDR pool we want in IPAM
    # so operators can see who's leasing what.
    assert _is_ipam_relevant_purpose("remote-user-vpn") is True


def test_vlan_only_relevant_for_vlan_row() -> None:
    # vlan-only networks have no L3 — no subnet CIDR at all — so the
    # reconciler is responsible for skipping them at the CIDR-parse
    # step. The purpose itself is "relevant" so the VLAN tag still
    # gets surfaced upstream.
    assert _is_ipam_relevant_purpose("vlan-only") is True


def test_wan_and_site_vpn_are_skipped() -> None:
    assert _is_ipam_relevant_purpose("wan") is False
    assert _is_ipam_relevant_purpose("site-vpn") is False
    assert _is_ipam_relevant_purpose("") is False


# ── _parse_client_row ─────────────────────────────────────────────────


def test_parse_client_active_with_live_ip() -> None:
    c = _parse_client_row(
        {
            "mac": "BC:24:11:E8:4A:3F",
            "ip": "10.0.0.50",
            "hostname": "laptop",
            "name": None,
            "network_id": "net1",
            "oui": "Apple",
            "is_wired": False,
            "last_seen": 1714780800,
        }
    )
    assert c is not None
    assert c.mac == "bc:24:11:e8:4a:3f"
    assert c.ip == "10.0.0.50"
    assert c.hostname == "laptop"
    assert c.network_id == "net1"
    assert c.oui == "Apple"
    assert c.is_wired is False
    assert c.fixed_ip is False


def test_parse_client_fixed_ip_takes_precedence_over_live() -> None:
    """Operator-configured DHCP fixed-IP should win — that's the IP
    the reservation reserves, regardless of what the device happens
    to be using right now (could be a live override or stale cache).
    """
    c = _parse_client_row(
        {
            "mac": "BC:24:11:E8:4A:3F",
            "ip": "10.0.0.99",  # currently leased
            "use_fixedip": True,
            "fixed_ip": "10.0.0.10",  # the reservation
            "name": "printer",
        }
    )
    assert c is not None
    assert c.ip == "10.0.0.10"
    assert c.fixed_ip is True
    assert c.name == "printer"


def test_parse_client_skips_row_without_mac() -> None:
    assert _parse_client_row({"ip": "10.0.0.5"}) is None
    assert _parse_client_row({"mac": ""}) is None
    assert _parse_client_row({"mac": "garbage"}) is None


def test_parse_client_handles_non_dict() -> None:
    assert _parse_client_row("not a dict") is None
    assert _parse_client_row(None) is None
    assert _parse_client_row([]) is None


def test_parse_client_uses_fixed_ip_only_when_use_fixedip_set() -> None:
    """``fixed_ip`` populated but ``use_fixedip=False`` means the
    operator turned the reservation off — fall back to the live IP.
    """
    c = _parse_client_row(
        {
            "mac": "BC:24:11:E8:4A:3F",
            "ip": "10.0.0.99",
            "use_fixedip": False,
            "fixed_ip": "10.0.0.10",
        }
    )
    assert c is not None
    assert c.ip == "10.0.0.99"
    assert c.fixed_ip is False


def test_parse_client_with_no_ip() -> None:
    # ``rest/user`` rows for offline devices have no live IP. The row
    # is still parsed (we'll skip it later if no ip resolves).
    c = _parse_client_row({"mac": "BC:24:11:E8:4A:3F", "hostname": "ghost"})
    assert c is not None
    assert c.ip is None
    assert c.hostname == "ghost"
