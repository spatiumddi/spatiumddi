"""AWS connector — EC2 / EIP / ELBv2 / classic-ELB → :class:`CloudInventory`.

Translates one AWS account's networking surface into the provider-neutral
shapes from :mod:`app.services.cloud.base`:

* ``ec2:DescribeVpcs``      → :class:`CloudNetwork` (one per VPC, all
  associated CIDRs flattened into ``cidrs``).
* ``ec2:DescribeSubnets``   → :class:`CloudSubnet`.
* ``ec2:DescribeInstances`` → :class:`CloudInstance` + per-ENI
  :class:`CloudNic`.
* ``ec2:DescribeAddresses`` → :class:`CloudPublicIP` (Elastic IPs).
* ``elasticloadbalancing v2:DescribeLoadBalancers`` → NLB static IPs as
  :class:`CloudLoadBalancer`; ALBs are DNS-name-only and skipped with a
  warning. Classic ELBs (``elb``) are likewise DNS-only and skipped.

AWS scopes by **region**: ``self.regions`` lists the regions to walk; an
empty list means "discover every enabled region" via
``ec2:DescribeRegions`` in ``us-east-1``. Per-region failures are
collected into :attr:`CloudInventory.warnings` and the walk continues —
only an auth / bootstrap failure (region discovery, STS) raises
:class:`CloudConnectorError`.

boto3 is imported lazily inside :meth:`_client` so importing this module
never hard-fails when the optional SDK is absent, and tests can monkeypatch
``AWSConnector._client`` to return a stub. Every blocking boto3 call runs in
``asyncio.to_thread`` because the connector's public methods are ``async``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.services.cloud.base import (
    CloudConnector,
    CloudConnectorError,
    CloudInstance,
    CloudInventory,
    CloudLoadBalancer,
    CloudNetwork,
    CloudNic,
    CloudProbeResult,
    CloudPublicIP,
    CloudSubnet,
)

logger = structlog.get_logger(__name__)

# Region used for account-scoped / region-discovery calls. STS + the
# ``ec2:DescribeRegions`` bootstrap both work against any enabled region;
# us-east-1 is the universally-enabled default.
_BOOTSTRAP_REGION = "us-east-1"


def _name_from_tags(tags: Any, fallback: str) -> str:
    """Return the ``Name`` tag value from an AWS ``[{Key,Value}]`` list.

    AWS tags arrive as ``[{"Key": "Name", "Value": "web-1"}, ...]``.
    Returns ``fallback`` when the list is absent / malformed / has no
    ``Name`` entry, so every normalised row always carries a usable name.
    """
    if not isinstance(tags, list):
        return fallback
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        if tag.get("Key") == "Name":
            value = tag.get("Value")
            if isinstance(value, str) and value:
                return value
    return fallback


class AWSConnector(CloudConnector):
    """Connector for a single AWS account (``credentials`` = access keys)."""

    provider = "aws"

    # ── boto3 client factory ─────────────────────────────────────────
    #
    # Isolated so tests can monkeypatch it with a stub returning canned
    # AWS-shaped dicts. boto3 is imported here (lazily) rather than at
    # module top level — the only place the SDK is touched.

    def _client(self, service: str, region: str) -> Any:
        """Build a boto3 client for ``service`` in ``region``.

        Raises :class:`CloudConnectorError` when boto3 is not installed
        so the caller surfaces a clean operator message instead of a raw
        ``ModuleNotFoundError``.
        """
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional dep guard
            raise CloudConnectorError(
                "boto3 is not installed; the AWS connector is unavailable."
            ) from exc
        return boto3.client(
            service,
            region_name=region,
            aws_access_key_id=self.credentials.get("access_key_id"),
            aws_secret_access_key=self.credentials.get("secret_access_key"),
        )

    @staticmethod
    def _botocore_errors() -> tuple[type[Exception], ...]:
        """Expected-failure botocore exception classes, resolved lazily.

        Returns ``(ClientError, EndpointConnectionError, NoCredentialsError,
        BotoCoreError)``. Falls back to an empty tuple when botocore is
        absent so a bare ``except`` clause built from it simply matches
        nothing (the import-guard in :meth:`_client` has already raised).
        """
        try:
            from botocore.exceptions import (
                BotoCoreError,
                ClientError,
                EndpointConnectionError,
                NoCredentialsError,
            )
        except ImportError:  # pragma: no cover - optional dep guard
            return ()
        return (ClientError, EndpointConnectionError, NoCredentialsError, BotoCoreError)

    # ── region resolution ────────────────────────────────────────────

    async def _resolve_regions(self) -> list[str]:
        """The regions to walk: explicit ``self.regions`` or every enabled.

        An empty ``self.regions`` triggers ``ec2:DescribeRegions`` in the
        bootstrap region (``AllRegions=False`` so only opted-in regions
        come back). Raises :class:`CloudConnectorError` on auth failure —
        without a region list there is nothing to iterate.
        """
        if self.regions:
            return list(self.regions)
        try:
            ec2 = self._client("ec2", _BOOTSTRAP_REGION)
            resp = await asyncio.to_thread(ec2.describe_regions, AllRegions=False)
        except self._botocore_errors() as exc:
            raise CloudConnectorError(f"AWS region discovery failed: {exc}") from exc
        regions = [
            str(r["RegionName"])
            for r in resp.get("Regions", [])
            if isinstance(r, dict) and r.get("RegionName")
        ]
        return sorted(regions)

    # ── probe ─────────────────────────────────────────────────────────

    async def probe(self) -> CloudProbeResult:
        """Cheap auth check: STS caller identity + VPC count in one region.

        Resolves the account id via ``sts:GetCallerIdentity`` and counts
        VPCs in the first resolved region. Returns
        ``CloudProbeResult(ok=False, ...)`` on any expected boto3 failure
        rather than raising.
        """
        errors = self._botocore_errors()
        try:
            sts = self._client("sts", _BOOTSTRAP_REGION)
            identity = await asyncio.to_thread(sts.get_caller_identity)
            account_id = str(identity.get("Account") or "") or None

            regions = await self._resolve_regions()
            network_count: int | None = None
            if regions:
                ec2 = self._client("ec2", regions[0])
                vpcs = await asyncio.to_thread(ec2.describe_vpcs)
                network_count = len(vpcs.get("Vpcs", []))
        except CloudConnectorError as exc:
            # boto3 missing or region discovery failed — already a clean
            # message; surface it as a probe failure, not a crash.
            return CloudProbeResult(ok=False, message=str(exc))
        except errors as exc:  # type: ignore[misc]  # tuple may be () when botocore absent
            return CloudProbeResult(ok=False, message=f"AWS probe failed: {exc}")
        return CloudProbeResult(
            ok=True,
            message=f"Connected to AWS account {account_id}",
            account_id=account_id,
            network_count=network_count,
        )

    # ── inventory ───────────────────────────────────────────────────

    async def fetch_inventory(
        self,
        *,
        include_stopped: bool = False,
        include_load_balancers: bool = True,
    ) -> CloudInventory:
        """Pull the full normalised inventory across every resolved region.

        Auth / region-discovery failures raise
        :class:`CloudConnectorError`. A single region failing mid-walk is
        recorded in :attr:`CloudInventory.warnings` and the remaining
        regions are still processed.
        """
        errors = self._botocore_errors()

        # The account id comes from STS once (region-independent). A
        # failure here is fatal — every IPAM row needs the account scope.
        try:
            sts = self._client("sts", _BOOTSTRAP_REGION)
            identity = await asyncio.to_thread(sts.get_caller_identity)
            account_id = str(identity.get("Account") or "")
        except errors as exc:  # type: ignore[misc]
            raise CloudConnectorError(f"AWS authentication failed: {exc}") from exc

        regions = await self._resolve_regions()
        inv = CloudInventory(account_id=account_id)

        for region in regions:
            try:
                await self._fetch_region(
                    inv,
                    region,
                    include_stopped=include_stopped,
                    include_load_balancers=include_load_balancers,
                )
            except errors as exc:  # type: ignore[misc]
                # Per-region failure (e.g. a disabled / unreachable
                # region) shouldn't sink the whole sweep — warn + move on.
                logger.warning("aws_region_fetch_failed", region=region, error=str(exc))
                inv.warnings.append(f"region {region}: {exc}")
                # #430 — mark this scope failed so the reconciler skips the
                # absence-delete pass; a region throttle after subnets land
                # must not purge the region's instance IPs.
                inv.failed_scopes.append(f"region {region}")

        return inv

    async def _fetch_region(
        self,
        inv: CloudInventory,
        region: str,
        *,
        include_stopped: bool,
        include_load_balancers: bool,
    ) -> None:
        """Append one region's networks / subnets / instances / IPs / LBs.

        Mutates ``inv`` in place. Raises botocore exceptions on failure;
        the caller turns those into per-region warnings.
        """
        ec2 = self._client("ec2", region)

        # VPCs → CloudNetwork. Every associated CIDR (state ``associated``)
        # becomes one entry in ``cidrs``; the reconciler creates one
        # IPBlock per CIDR.
        vpcs = await asyncio.to_thread(ec2.describe_vpcs)
        for vpc in vpcs.get("Vpcs", []):
            if not isinstance(vpc, dict):
                continue
            vpc_id = str(vpc.get("VpcId") or "")
            if not vpc_id:
                continue
            cidrs: list[str] = []
            for assoc in vpc.get("CidrBlockAssociationSet", []):
                if not isinstance(assoc, dict):
                    continue
                state = assoc.get("CidrBlockState", {})
                if isinstance(state, dict) and state.get("State") != "associated":
                    continue
                cidr = assoc.get("CidrBlock")
                if isinstance(cidr, str) and cidr:
                    cidrs.append(cidr)
            inv.networks.append(
                CloudNetwork(
                    id=vpc_id,
                    name=_name_from_tags(vpc.get("Tags"), vpc_id),
                    cidrs=tuple(cidrs),
                    region=region,
                )
            )

        # Subnets → CloudSubnet. ``region`` is reported as the AZ so the
        # discovery snapshot keeps the placement detail.
        subnets = await asyncio.to_thread(ec2.describe_subnets)
        for subnet in subnets.get("Subnets", []):
            if not isinstance(subnet, dict):
                continue
            subnet_id = str(subnet.get("SubnetId") or "")
            cidr = str(subnet.get("CidrBlock") or "")
            if not subnet_id or not cidr:
                continue
            inv.subnets.append(
                CloudSubnet(
                    id=subnet_id,
                    name=_name_from_tags(subnet.get("Tags"), subnet_id),
                    network_id=str(subnet.get("VpcId") or ""),
                    cidr=cidr,
                    region=str(subnet.get("AvailabilityZone") or region),
                )
            )

        # Instances → CloudInstance + per-ENI CloudNic. The running-only
        # default is enforced server-side with a state-name filter so we
        # don't transfer stopped instances we'll just discard.
        kwargs: dict[str, Any] = {}
        if not include_stopped:
            kwargs["Filters"] = [{"Name": "instance-state-name", "Values": ["running"]}]
        reservations = await asyncio.to_thread(ec2.describe_instances, **kwargs)
        for reservation in reservations.get("Reservations", []):
            if not isinstance(reservation, dict):
                continue
            for instance in reservation.get("Instances", []):
                if not isinstance(instance, dict):
                    continue
                instance_id = str(instance.get("InstanceId") or "")
                if not instance_id:
                    continue
                state = instance.get("State", {})
                state_name = state.get("Name") if isinstance(state, dict) else None
                nics: list[CloudNic] = []
                for eni in instance.get("NetworkInterfaces", []):
                    if not isinstance(eni, dict):
                        continue
                    private_ip = eni.get("PrivateIpAddress")
                    if not isinstance(private_ip, str) or not private_ip:
                        continue
                    assoc = eni.get("Association", {})
                    public_ip = assoc.get("PublicIp") if isinstance(assoc, dict) else None
                    nics.append(
                        CloudNic(
                            private_ip=private_ip,
                            public_ip=public_ip if isinstance(public_ip, str) else None,
                            mac=(
                                eni.get("MacAddress")
                                if isinstance(eni.get("MacAddress"), str)
                                else None
                            ),
                        )
                    )
                inv.instances.append(
                    CloudInstance(
                        id=instance_id,
                        name=_name_from_tags(instance.get("Tags"), instance_id),
                        running=state_name == "running",
                        nics=tuple(nics),
                        region=region,
                    )
                )

        # Elastic IPs → CloudPublicIP. An EIP is "attached" when it is
        # associated with either an instance or a network interface.
        addresses = await asyncio.to_thread(ec2.describe_addresses)
        for addr in addresses.get("Addresses", []):
            if not isinstance(addr, dict):
                continue
            public_ip = addr.get("PublicIp")
            if not isinstance(public_ip, str) or not public_ip:
                continue
            inv.public_ips.append(
                CloudPublicIP(
                    address=public_ip,
                    name=_name_from_tags(addr.get("Tags"), str(addr.get("AllocationId") or "")),
                    attached=bool(addr.get("InstanceId") or addr.get("NetworkInterfaceId")),
                )
            )

        if include_load_balancers:
            await self._fetch_load_balancers(inv, region)

    async def _fetch_load_balancers(self, inv: CloudInventory, region: str) -> None:
        """Append NLB static frontend IPs; warn-and-skip DNS-only LBs.

        ELBv2 NLBs expose fixed IPs under
        ``AvailabilityZones[].LoadBalancerAddresses[].IpAddress``. ELBv2
        ALBs and classic ELBs are DNS-name-only (their public IPs float),
        so there is no stable frontend IP to mirror — those are recorded
        as warnings and skipped.
        """
        # ELBv2 — NLB (network) carries static IPs; ALB (application) does not.
        elbv2 = self._client("elbv2", region)
        v2 = await asyncio.to_thread(elbv2.describe_load_balancers)
        for lb in v2.get("LoadBalancers", []):
            if not isinstance(lb, dict):
                continue
            lb_arn = str(lb.get("LoadBalancerArn") or "")
            lb_name = str(lb.get("LoadBalancerName") or lb_arn)
            lb_type = lb.get("Type")
            if lb_type == "network":
                frontend_ips: list[str] = []
                for az in lb.get("AvailabilityZones", []):
                    if not isinstance(az, dict):
                        continue
                    for lba in az.get("LoadBalancerAddresses", []):
                        if not isinstance(lba, dict):
                            continue
                        ip = lba.get("IpAddress")
                        if isinstance(ip, str) and ip:
                            frontend_ips.append(ip)
                inv.load_balancers.append(
                    CloudLoadBalancer(
                        id=lb_arn or lb_name,
                        name=lb_name,
                        frontend_ips=tuple(frontend_ips),
                        region=region,
                    )
                )
            else:
                # ALB / gateway LB — DNS-name-only, no fixed frontend IP.
                inv.warnings.append(
                    f"region {region}: load balancer {lb_name!r} is DNS-name-only "
                    f"(type {lb_type!r}); no static frontend IP to mirror — skipped."
                )

        # Classic ELB — always DNS-name-only.
        elb = self._client("elb", region)
        classic = await asyncio.to_thread(elb.describe_load_balancers)
        for lb in classic.get("LoadBalancerDescriptions", []):
            if not isinstance(lb, dict):
                continue
            lb_name = str(lb.get("LoadBalancerName") or "")
            inv.warnings.append(
                f"region {region}: classic load balancer {lb_name!r} is "
                f"DNS-name-only; no static frontend IP to mirror — skipped."
            )


__all__ = ["AWSConnector"]
