"""Unit tests for the AWS cloud connector.

These tests are fully offline (tier-3 provider, no test account): every
boto3 call is served by a :class:`_StubClient` returning canned
AWS-shaped dicts, wired in by monkeypatching ``AWSConnector._client``.
botocore is not installed in CI, so the connector's lazy
``_botocore_errors()`` returns an empty tuple — the success paths never
exercise an ``except`` clause built from it, and the failure-path tests
inject their own fake exception classes.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.cloud.aws import AWSConnector, _name_from_tags
from app.services.cloud.base import CloudConnectorError


class _StubClient:
    """A single boto3 client whose method calls return canned dicts.

    Constructed with a mapping of ``method_name -> response dict`` (or a
    callable taking the call kwargs). Any method not in the map returns
    an empty dict, mirroring boto3's "absent key" behaviour for the
    paginated list shapes the connector reads with ``.get(..., [])``.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def _call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            value = self._responses.get(name, {})
            return value(kwargs) if callable(value) else value

        return _call


def _make_connector(
    clients: dict[str, _StubClient],
    *,
    regions: list[str] | None = None,
) -> AWSConnector:
    """Build an AWSConnector with ``_client`` patched to serve ``clients``.

    ``clients`` is keyed by service name (``"ec2"``, ``"sts"``, ...);
    every region resolves to the same stub for that service. ``regions``
    is passed through verbatim — an explicit empty list keeps the
    region-discovery path so ``test_..._discovers_all_regions`` works.
    """
    connector = AWSConnector(
        credentials={"access_key_id": "AKIA", "secret_access_key": "secret"},
        provider_config={},
        regions=["us-east-1"] if regions is None else regions,
    )

    def _client(service: str, region: str) -> _StubClient:  # noqa: ARG001
        return clients[service]

    connector._client = _client  # type: ignore[method-assign]
    return connector


# ── _name_from_tags ──────────────────────────────────────────────────


def test_name_from_tags_returns_name_value() -> None:
    tags = [{"Key": "env", "Value": "prod"}, {"Key": "Name", "Value": "web-1"}]
    assert _name_from_tags(tags, "i-fallback") == "web-1"


def test_name_from_tags_falls_back_when_no_name() -> None:
    assert _name_from_tags([{"Key": "env", "Value": "prod"}], "i-123") == "i-123"


def test_name_from_tags_falls_back_on_none() -> None:
    assert _name_from_tags(None, "vpc-fallback") == "vpc-fallback"


# ── fetch_inventory ──────────────────────────────────────────────────


def _ec2_full() -> _StubClient:
    """An EC2 stub with a 2-CIDR VPC, one subnet, a running + a stopped
    instance, and one Elastic IP.
    """
    return _StubClient(
        {
            "describe_vpcs": {
                "Vpcs": [
                    {
                        "VpcId": "vpc-aaa",
                        "Tags": [{"Key": "Name", "Value": "main-vpc"}],
                        "CidrBlockAssociationSet": [
                            {
                                "CidrBlock": "10.0.0.0/16",
                                "CidrBlockState": {"State": "associated"},
                            },
                            {
                                "CidrBlock": "10.1.0.0/16",
                                "CidrBlockState": {"State": "associated"},
                            },
                            {
                                # disassociating CIDR is dropped
                                "CidrBlock": "10.2.0.0/16",
                                "CidrBlockState": {"State": "disassociating"},
                            },
                        ],
                    }
                ]
            },
            "describe_subnets": {
                "Subnets": [
                    {
                        "SubnetId": "subnet-111",
                        "VpcId": "vpc-aaa",
                        "CidrBlock": "10.0.1.0/24",
                        "AvailabilityZone": "us-east-1a",
                        "Tags": [{"Key": "Name", "Value": "web-subnet"}],
                    }
                ]
            },
            # describe_instances honours the running-only filter: when the
            # connector passes a state filter, return only the running box.
            "describe_instances": _instances_response,
            "describe_addresses": {
                "Addresses": [
                    {
                        "PublicIp": "52.1.2.3",
                        "AllocationId": "eipalloc-1",
                        "InstanceId": "i-running",
                        "Tags": [{"Key": "Name", "Value": "nat-eip"}],
                    }
                ]
            },
        }
    )


def _instances_response(kwargs: dict[str, Any]) -> dict[str, Any]:
    running = {
        "InstanceId": "i-running",
        "State": {"Name": "running"},
        "Tags": [{"Key": "Name", "Value": "app-1"}],
        "NetworkInterfaces": [
            {
                "PrivateIpAddress": "10.0.1.10",
                "MacAddress": "0a:1b:2c:3d:4e:5f",
                "Association": {"PublicIp": "52.9.9.9"},
            }
        ],
    }
    stopped = {
        "InstanceId": "i-stopped",
        "State": {"Name": "stopped"},
        "Tags": [{"Key": "Name", "Value": "app-2"}],
        "NetworkInterfaces": [{"PrivateIpAddress": "10.0.1.11"}],
    }
    # When a state filter is present (include_stopped=False), AWS returns
    # only the running instance; otherwise both.
    if kwargs.get("Filters"):
        return {"Reservations": [{"Instances": [running]}]}
    return {"Reservations": [{"Instances": [running, stopped]}]}


def _elb_stubs() -> dict[str, _StubClient]:
    """ELBv2 stub with one NLB (static IP) + one ALB (DNS-only), plus a
    classic ELB stub.
    """
    elbv2 = _StubClient(
        {
            "describe_load_balancers": {
                "LoadBalancers": [
                    {
                        "LoadBalancerArn": "arn:nlb",
                        "LoadBalancerName": "net-lb",
                        "Type": "network",
                        "AvailabilityZones": [
                            {"LoadBalancerAddresses": [{"IpAddress": "203.0.113.5"}]}
                        ],
                    },
                    {
                        "LoadBalancerArn": "arn:alb",
                        "LoadBalancerName": "app-lb",
                        "Type": "application",
                        "AvailabilityZones": [],
                    },
                ]
            }
        }
    )
    elb = _StubClient(
        {
            "describe_load_balancers": {
                "LoadBalancerDescriptions": [{"LoadBalancerName": "classic-lb"}]
            }
        }
    )
    return {"elbv2": elbv2, "elb": elb}


async def test_fetch_inventory_normalizes_running_only() -> None:
    clients = {
        "sts": _StubClient({"get_caller_identity": {"Account": "123456789012"}}),
        "ec2": _ec2_full(),
        **_elb_stubs(),
    }
    connector = _make_connector(clients, regions=["us-east-1"])

    inv = await connector.fetch_inventory(include_stopped=False)

    assert inv.account_id == "123456789012"

    # 2-CIDR VPC: the disassociating third CIDR is dropped.
    assert len(inv.networks) == 1
    net = inv.networks[0]
    assert net.id == "vpc-aaa"
    assert net.name == "main-vpc"
    assert net.cidrs == ("10.0.0.0/16", "10.1.0.0/16")
    assert net.region == "us-east-1"

    # Subnet carries the AZ as its region.
    assert len(inv.subnets) == 1
    subnet = inv.subnets[0]
    assert subnet.id == "subnet-111"
    assert subnet.network_id == "vpc-aaa"
    assert subnet.cidr == "10.0.1.0/24"
    assert subnet.region == "us-east-1a"

    # Only the running instance — the stopped one is filtered out.
    assert len(inv.instances) == 1
    inst = inv.instances[0]
    assert inst.id == "i-running"
    assert inst.name == "app-1"
    assert inst.running is True
    assert len(inst.nics) == 1
    nic = inst.nics[0]
    assert nic.private_ip == "10.0.1.10"
    assert nic.public_ip == "52.9.9.9"
    assert nic.mac == "0a:1b:2c:3d:4e:5f"

    # Elastic IP, attached because it references an instance.
    assert len(inv.public_ips) == 1
    eip = inv.public_ips[0]
    assert eip.address == "52.1.2.3"
    assert eip.name == "nat-eip"
    assert eip.attached is True


async def test_fetch_inventory_include_stopped_returns_both() -> None:
    clients = {
        "sts": _StubClient({"get_caller_identity": {"Account": "123456789012"}}),
        "ec2": _ec2_full(),
        **_elb_stubs(),
    }
    connector = _make_connector(clients, regions=["us-east-1"])

    inv = await connector.fetch_inventory(include_stopped=True)

    ids = {i.id for i in inv.instances}
    assert ids == {"i-running", "i-stopped"}
    stopped = next(i for i in inv.instances if i.id == "i-stopped")
    assert stopped.running is False


async def test_fetch_inventory_nlb_ip_alb_and_classic_warn() -> None:
    clients = {
        "sts": _StubClient({"get_caller_identity": {"Account": "123456789012"}}),
        "ec2": _ec2_full(),
        **_elb_stubs(),
    }
    connector = _make_connector(clients, regions=["us-east-1"])

    inv = await connector.fetch_inventory(include_load_balancers=True)

    # The NLB is mirrored with its static frontend IP.
    assert len(inv.load_balancers) == 1
    nlb = inv.load_balancers[0]
    assert nlb.name == "net-lb"
    assert nlb.frontend_ips == ("203.0.113.5",)

    # ALB + classic ELB both produce a warning and are skipped.
    joined = "\n".join(inv.warnings)
    assert "app-lb" in joined
    assert "classic-lb" in joined
    assert len([w for w in inv.warnings if "DNS-name-only" in w]) == 2


async def test_fetch_inventory_skips_load_balancers_when_disabled() -> None:
    clients = {
        "sts": _StubClient({"get_caller_identity": {"Account": "123456789012"}}),
        "ec2": _ec2_full(),
        **_elb_stubs(),
    }
    connector = _make_connector(clients, regions=["us-east-1"])

    inv = await connector.fetch_inventory(include_load_balancers=False)

    assert inv.load_balancers == []
    # No DNS-only LB warnings either since elbv2/elb were never called.
    assert not any("DNS-name-only" in w for w in inv.warnings)


async def test_fetch_inventory_per_region_failure_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A botocore error in one region becomes a warning, not a raise."""

    class _FakeClientError(Exception):
        pass

    # Make _botocore_errors match our fake so the per-region except hits.
    monkeypatch.setattr(
        AWSConnector,
        "_botocore_errors",
        staticmethod(lambda: (_FakeClientError,)),
    )

    good_ec2 = _ec2_full()
    sts = _StubClient({"get_caller_identity": {"Account": "123456789012"}})

    def boom(_kwargs: dict[str, Any]) -> Any:
        raise _FakeClientError("AccessDenied in eu-west-1")

    bad_ec2 = _StubClient({"describe_vpcs": boom})
    elbs = _elb_stubs()

    connector = AWSConnector(
        credentials={"access_key_id": "AKIA", "secret_access_key": "secret"},
        provider_config={},
        regions=["us-east-1", "eu-west-1"],
    )

    def _client(service: str, region: str) -> _StubClient:
        if service == "sts":
            return sts
        if service == "ec2":
            return good_ec2 if region == "us-east-1" else bad_ec2
        return elbs[service]

    connector._client = _client  # type: ignore[method-assign]

    inv = await connector.fetch_inventory()

    # us-east-1 still produced its VPC; eu-west-1 produced a warning.
    assert [n.id for n in inv.networks] == ["vpc-aaa"]
    assert any("eu-west-1" in w and "AccessDenied" in w for w in inv.warnings)


async def test_fetch_inventory_auth_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeAuthError(Exception):
        pass

    monkeypatch.setattr(
        AWSConnector,
        "_botocore_errors",
        staticmethod(lambda: (_FakeAuthError,)),
    )

    def boom(_kwargs: dict[str, Any]) -> Any:
        raise _FakeAuthError("InvalidClientTokenId")

    sts = _StubClient({"get_caller_identity": boom})
    connector = _make_connector({"sts": sts}, regions=["us-east-1"])

    with pytest.raises(CloudConnectorError, match="authentication failed"):
        await connector.fetch_inventory()


async def test_fetch_inventory_discovers_all_regions_when_empty() -> None:
    """An empty region list triggers ec2:DescribeRegions in the bootstrap
    region, and the returned regions are walked.
    """
    sts = _StubClient({"get_caller_identity": {"Account": "123456789012"}})
    # The bootstrap ec2 client answers describe_regions; the same stub
    # also answers the per-region describe_* calls (single VPC).
    ec2 = _StubClient(
        {
            "describe_regions": {
                "Regions": [
                    {"RegionName": "us-west-2"},
                    {"RegionName": "us-east-1"},
                ]
            },
            "describe_vpcs": {
                "Vpcs": [
                    {
                        "VpcId": "vpc-z",
                        "CidrBlockAssociationSet": [
                            {
                                "CidrBlock": "172.16.0.0/16",
                                "CidrBlockState": {"State": "associated"},
                            }
                        ],
                    }
                ]
            },
        }
    )
    connector = _make_connector({"sts": sts, "ec2": ec2, **_elb_stubs()}, regions=[])

    inv = await connector.fetch_inventory(include_load_balancers=False)

    # Two discovered regions × one VPC each (the same ec2 stub).
    assert [n.id for n in inv.networks] == ["vpc-z", "vpc-z"]
    assert {n.region for n in inv.networks} == {"us-west-2", "us-east-1"}


# ── probe ─────────────────────────────────────────────────────────────


async def test_probe_ok() -> None:
    sts = _StubClient({"get_caller_identity": {"Account": "999988887777"}})
    ec2 = _StubClient({"describe_vpcs": {"Vpcs": [{"VpcId": "vpc-1"}, {"VpcId": "vpc-2"}]}})
    connector = _make_connector({"sts": sts, "ec2": ec2}, regions=["us-east-1"])

    result = await connector.probe()

    assert result.ok is True
    assert result.account_id == "999988887777"
    assert result.network_count == 2


async def test_probe_failure_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeError(Exception):
        pass

    monkeypatch.setattr(
        AWSConnector,
        "_botocore_errors",
        staticmethod(lambda: (_FakeError,)),
    )

    def boom(_kwargs: dict[str, Any]) -> Any:
        raise _FakeError("SignatureDoesNotMatch")

    sts = _StubClient({"get_caller_identity": boom})
    connector = _make_connector({"sts": sts}, regions=["us-east-1"])

    result = await connector.probe()

    assert result.ok is False
    assert "SignatureDoesNotMatch" in result.message
    assert result.account_id is None


async def test_probe_handles_missing_boto3() -> None:
    """When _client raises CloudConnectorError (boto3 absent), probe
    returns ok=False rather than propagating.
    """
    connector = AWSConnector(
        credentials={"access_key_id": "AKIA", "secret_access_key": "secret"},
        provider_config={},
        regions=["us-east-1"],
    )

    def _client(service: str, region: str) -> Any:  # noqa: ARG001
        raise CloudConnectorError("boto3 is not installed; the AWS connector is unavailable.")

    connector._client = _client  # type: ignore[method-assign]

    result = await connector.probe()

    assert result.ok is False
    assert "boto3 is not installed" in result.message
