"""Tests for the curated VoIP-phone vendor matcher (issue #112 phase 3).

The matcher is the canonical source of truth for the Phone-icon flag
in the IPAM list, the IP detail modal, and the DHCP lease table — all
three paths feed through ``is_voip_phone_vendor()``. Behaviour we lock
in here:

* Case-insensitive substring match against the IEEE OUI vendor name
* The 9 vendor recipes shipped in phase 1 each map to a true result
  for at least one canonical OUI string
* Generic Cisco strings are deliberately *not* matched (Cisco's OUI
  registrations span both routers and phones; option-150 fences in
  phone profiles do that disambiguation reliably)
* Empty / null vendor returns False without raising
"""

from __future__ import annotations

import pytest

from app.services.oui import is_voip_phone_vendor


@pytest.mark.parametrize(
    "vendor",
    [
        "Polycom",
        "Polycom, Inc.",
        "Polycom Inc",
        "POLYCOM, INC",  # case variation
        "Yealink Network Technology Co.,Ltd",
        "Mitel Corporation",
        "AASTRA TELECOM SWEDEN AB",  # rebrand-era IEEE entry
        "Avaya Inc",
        "snom technology AG",
        "Grandstream Networks, Inc.",
        "Cisco-Linksys, LLC",
        "Cisco SPA-303G",
        "AudioCodes Ltd.",
        "Spectralink Corporation",
        "Fanvil Technology Co., Ltd.",
        "Obihai Technology, Inc.",
        "Sangoma Technologies",
        "DIGIUM",
        "Panasonic Communications Co., Ltd.",
    ],
)
def test_voip_phone_matches_curated_vendors(vendor: str) -> None:
    assert is_voip_phone_vendor(vendor) is True


@pytest.mark.parametrize(
    "vendor",
    [
        # Cisco generic strings — deliberately NOT matched. Operator
        # disambiguates phone vs router via option-150 / option-66
        # fences in the phone profile (#112 phase 1).
        "Cisco Systems",
        "Cisco Meraki",
        # Random consumer / IT vendors that should NOT trip the flag.
        "Apple, Inc.",
        "Raspberry Pi Foundation",
        "Intel Corporate",
        "Dell Inc.",
        "Hewlett Packard Enterprise",
        "Microsoft Corporation",
        "Samsung Electronics",
        "VMware, Inc.",
        "Ubiquiti Networks Inc.",
    ],
)
def test_voip_phone_does_not_match_non_phone_vendors(vendor: str) -> None:
    assert is_voip_phone_vendor(vendor) is False


def test_voip_phone_handles_empty_and_none() -> None:
    assert is_voip_phone_vendor(None) is False
    assert is_voip_phone_vendor("") is False
    assert is_voip_phone_vendor("   ") is False
