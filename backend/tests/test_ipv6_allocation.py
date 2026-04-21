"""Unit coverage for the v6 auto-allocation helpers.

Touches the pure-function surface that doesn't need the DB:
  * _eui64_from_mac — RFC 4291 §2.5.1 Modified EUI-64 derivation

Rationale: end-to-end exercise of _pick_next_available_ip requires a
live DB session + subnet + IPAddress rows, which lives in the broader
integration suite. The EUI-64 math is the piece with the subtle bits
(universal/local flip, FF:FE insertion), and a bad derivation here
silently hands out addresses that collide with SLAAC auto-config —
so pin the canonical examples from the RFC.
"""

from __future__ import annotations

import ipaddress

import pytest

from app.api.v1.ipam.router import _eui64_from_mac


class TestEUI64:
    def test_rfc4291_example(self) -> None:
        """RFC 4291 Appendix A: 00:AA:00:3F:2A:1C → 2AA:00FF:FE3F:2A1C.

        The universal/local bit flip on the first octet turns 0x00 into
        0x02, yielding the IID ``02AA:00FF:FE3F:2A1C``.
        """
        net = ipaddress.IPv6Network("2001:db8::/64")
        addr = _eui64_from_mac(net, "00:AA:00:3F:2A:1C")
        assert addr == ipaddress.IPv6Address("2001:db8::2aa:ff:fe3f:2a1c")

    def test_universal_bit_flip(self) -> None:
        """MAC with the universal bit set (first octet 0x02) clears it."""
        net = ipaddress.IPv6Network("fe80::/64")
        addr = _eui64_from_mac(net, "02:00:00:00:00:01")
        # 0x02 XOR 0x02 == 0x00
        assert addr == ipaddress.IPv6Address("fe80::ff:fe00:1")

    def test_loose_mac_format_accepted(self) -> None:
        """Separator-free + dash-separated MACs work too."""
        net = ipaddress.IPv6Network("2001:db8::/64")
        ref = _eui64_from_mac(net, "00:aa:00:3f:2a:1c")
        assert _eui64_from_mac(net, "00AA003F2A1C") == ref
        assert _eui64_from_mac(net, "00-aa-00-3f-2a-1c") == ref

    def test_non_slash_64_returns_none(self) -> None:
        """EUI-64 is only defined for 64-bit host parts."""
        net = ipaddress.IPv6Network("2001:db8::/80")
        assert _eui64_from_mac(net, "00:AA:00:3F:2A:1C") is None

    @pytest.mark.parametrize(
        "bad_mac",
        [
            "",
            "not a mac",
            "00:11:22:33:44",  # too short
            "00:11:22:33:44:55:66",  # too long
            "gg:gg:gg:gg:gg:gg",  # non-hex
        ],
    )
    def test_bad_mac_returns_none(self, bad_mac: str) -> None:
        net = ipaddress.IPv6Network("2001:db8::/64")
        assert _eui64_from_mac(net, bad_mac) is None
