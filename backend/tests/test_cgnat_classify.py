"""CGNAT (RFC 6598) awareness — issue #42.

Operator-facing classification only: a "CGNAT" badge + New-Subnet
hint driven off a computed property. These tests pin the classifier
edge cases and the ``SubnetResponse`` derivation (no DB — the field
is computed from the network CIDR, not a column).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.services.ipam.classify import is_cgnat_cidr


@pytest.mark.parametrize(
    "cidr,expected",
    [
        # Inside 100.64.0.0/10 (100.64.0.0 – 100.127.255.255).
        ("100.64.0.0/10", True),  # the range itself
        ("100.64.5.0/24", True),  # typical leaf
        ("100.127.255.0/24", True),  # top of the range
        ("100.100.100.100/32", True),  # single host
        ("100.96.0.0/12", True),  # mid-range block
        # Outside.
        ("100.63.255.0/24", False),  # one below the range
        ("100.128.0.0/24", False),  # one above the range
        ("10.0.1.0/24", False),  # RFC 1918
        ("192.168.1.0/24", False),  # RFC 1918
        ("100.0.0.0/8", False),  # supernet that merely overlaps — not flagged
        ("8.8.8.0/24", False),  # public
        # IPv6 is never CGNAT.
        ("2001:db8::/64", False),
        ("fc00::/7", False),
        # Malformed input degrades to False, never raises.
        ("not-a-cidr", False),
        ("", False),
        # A bare IP coerces to /32 (strict=False); 100.64.0.0/32 is
        # still inside CGNAT, so this is correctly True. SubnetResponse
        # always carries a real CIDR, so this is just edge robustness.
        ("100.64.0.0", True),
    ],
)
def test_is_cgnat_cidr(cidr: str, expected: bool) -> None:
    assert is_cgnat_cidr(cidr) is expected


def _subnet_response(network: str):
    from app.api.v1.ipam.router import SubnetResponse

    return SubnetResponse(
        id=uuid.uuid4(),
        space_id=uuid.uuid4(),
        block_id=uuid.uuid4(),
        network=network,
        name="t",
        description="",
        vlan_id=None,
        vxlan_id=None,
        gateway=None,
        status="active",
        utilization_percent=0.0,
        total_ips=0,
        allocated_ips=0,
        dns_servers=None,
        domain_name=None,
        tags={},
        custom_fields={},
        dns_group_ids=None,
        dns_zone_id=None,
        dns_additional_zone_ids=None,
        dns_inherit_settings=True,
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )


def test_subnet_response_derives_cgnat_flag() -> None:
    assert _subnet_response("100.64.10.0/24").is_cgnat is True
    assert _subnet_response("10.0.1.0/24").is_cgnat is False
    # IPv6 subnet — never CGNAT, must not raise on the derivation.
    assert _subnet_response("2001:db8:1::/64").is_cgnat is False
