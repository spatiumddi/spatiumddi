"""Azure connector for the Cloud infrastructure mirror (#37).

Translates the Azure Resource Manager (ARM) SDK responses into the
provider-neutral :class:`~app.services.cloud.base.CloudInventory` the
shared reconciler consumes. One VNet → :class:`CloudNetwork` (one
``IPBlock`` per address-space prefix), each VNet subnet →
:class:`CloudSubnet`, each VM → :class:`CloudInstance` with its NIC
private/public IPs + MAC, every public IP → :class:`CloudPublicIP`, and
(optionally) each load balancer frontend → :class:`CloudLoadBalancer`.

Credentials (decrypted from ``CloudEndpoint.credentials_encrypted``) are a
service-principal triple ``{"tenant_id", "client_id", "client_secret"}``.
Non-secret routing (``provider_config``) carries the subscription scope as
``{"subscription_ids": [str, ...]}``; ``regions`` (Azure *locations*) is an
allow-list applied to every resource — empty means all locations.

The Azure SDK is imported lazily inside the factory layer so importing
this module never hard-fails on the optional ``azure-*`` packages, and
tests can monkeypatch :meth:`AzureConnector._credential` /
``_network_client`` / ``_compute_client`` to return stub clients. All
blocking SDK list calls run under :func:`asyncio.to_thread` because the
public methods are ``async`` (CLAUDE.md non-negotiable #2).
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


class AzureConnector(CloudConnector):
    """Mirror Azure VNets / subnets / VMs / public IPs / load balancers."""

    provider = "azure"

    # ── SDK factory layer (monkeypatched in tests) ─────────────────────
    #
    # Each factory lazy-imports its Azure package so a missing optional
    # dependency only bites when a real sync runs, never at import time.

    def _credential(self) -> Any:
        """Build a service-principal credential from ``self.credentials``."""
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(
            tenant_id=self.credentials["tenant_id"],
            client_id=self.credentials["client_id"],
            client_secret=self.credentials["client_secret"],
        )

    def _network_client(self, subscription_id: str) -> Any:
        """ARM network management client scoped to one subscription."""
        from azure.mgmt.network import NetworkManagementClient

        return NetworkManagementClient(self._credential(), subscription_id)

    def _compute_client(self, subscription_id: str) -> Any:
        """ARM compute management client scoped to one subscription."""
        from azure.mgmt.compute import ComputeManagementClient

        return ComputeManagementClient(self._credential(), subscription_id)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _rg_from_id(resource_id: str | None) -> str | None:
        """Extract the ``resourceGroups/<rg>`` segment from an ARM id.

        ARM ids look like
        ``/subscriptions/<sub>/resourceGroups/<rg>/providers/...``; the
        resource-group name is the token after the (case-insensitive)
        ``resourceGroups`` segment. Returns ``None`` when absent.
        """
        if not resource_id:
            return None
        parts = resource_id.strip("/").split("/")
        for idx, part in enumerate(parts):
            if part.lower() == "resourcegroups" and idx + 1 < len(parts):
                return parts[idx + 1]
        return None

    def _in_region(self, location: str | None) -> bool:
        """``True`` when ``location`` passes the (possibly empty) allow-list."""
        if not self.regions:
            return True
        return location in self.regions

    # ── Probe ──────────────────────────────────────────────────────────

    async def probe(self) -> CloudProbeResult:
        """Cheap auth + reachability check for the test-connection button.

        Lists the VNets in the first configured subscription, counting
        them. Expected auth / HTTP failures return ``ok=False`` rather
        than raising (CLAUDE.md probe contract in ``base.py``).
        """
        from azure.core.exceptions import (
            ClientAuthenticationError,
            HttpResponseError,
        )

        subscription_ids = list(self.provider_config.get("subscription_ids") or [])
        account_id = subscription_ids[0] if subscription_ids else self.credentials.get("tenant_id")

        if not subscription_ids:
            return CloudProbeResult(
                ok=False,
                message="No subscription_ids configured for the Azure endpoint.",
                account_id=account_id,
            )

        try:
            client = self._network_client(subscription_ids[0])
            vnets = await asyncio.to_thread(lambda: list(client.virtual_networks.list_all()))
        except (ClientAuthenticationError, HttpResponseError) as exc:
            return CloudProbeResult(
                ok=False,
                message=f"Azure authentication / API error: {exc}",
                account_id=account_id,
            )

        count = sum(1 for vnet in vnets if self._in_region(getattr(vnet, "location", None)))
        return CloudProbeResult(
            ok=True,
            message=f"Connected to subscription {subscription_ids[0]}; {count} VNet(s) visible.",
            account_id=account_id,
            network_count=count,
        )

    # ── Inventory ──────────────────────────────────────────────────────

    async def fetch_inventory(
        self,
        *,
        include_stopped: bool = False,
        include_load_balancers: bool = True,
    ) -> CloudInventory:
        """Pull the full normalised inventory across every subscription.

        Per-subscription auth / HTTP failures fold into
        :attr:`CloudInventory.warnings` so one bad subscription doesn't
        abort the whole sync. :class:`CloudConnectorError` is raised only
        when *nothing* could be fetched (e.g. credential build failed for
        every subscription).
        """
        subscription_ids = list(self.provider_config.get("subscription_ids") or [])
        account_id = subscription_ids[0] if subscription_ids else self.credentials.get("tenant_id")
        inventory = CloudInventory(account_id=account_id or "")

        if not subscription_ids:
            raise CloudConnectorError("No subscription_ids configured for the Azure endpoint.")

        succeeded = 0
        for subscription_id in subscription_ids:
            try:
                await self._fetch_subscription(
                    subscription_id,
                    inventory,
                    include_stopped=include_stopped,
                    include_load_balancers=include_load_balancers,
                )
                succeeded += 1
            except CloudConnectorError as exc:
                # Auth / HTTP failure for this subscription only — record
                # it and keep going so the others still reconcile.
                inventory.warnings.append(f"subscription {subscription_id}: {exc}")
                logger.warning(
                    "cloud.azure.subscription_failed",
                    subscription_id=subscription_id,
                    error=str(exc),
                )
                # #430 — mark this scope failed so the reconciler skips the
                # absence-delete pass; a failed subscription in a multi-sub
                # endpoint must not purge that subscription's rows.
                inventory.failed_scopes.append(f"subscription {subscription_id}")

        if succeeded == 0:
            raise CloudConnectorError(
                "Azure inventory fetch failed for every subscription: "
                + "; ".join(inventory.warnings)
            )
        return inventory

    async def _fetch_subscription(
        self,
        subscription_id: str,
        inventory: CloudInventory,
        *,
        include_stopped: bool,
        include_load_balancers: bool,
    ) -> None:
        """Append one subscription's resources into ``inventory`` in place."""
        from azure.core.exceptions import (
            ClientAuthenticationError,
            HttpResponseError,
        )

        try:
            network_client = self._network_client(subscription_id)
            compute_client = self._compute_client(subscription_id)

            vnets = await asyncio.to_thread(
                lambda: list(network_client.virtual_networks.list_all())
            )
            nics = await asyncio.to_thread(
                lambda: list(network_client.network_interfaces.list_all())
            )
            public_ips = await asyncio.to_thread(
                lambda: list(network_client.public_ip_addresses.list_all())
            )
            vms = await asyncio.to_thread(lambda: list(compute_client.virtual_machines.list_all()))
            lbs = (
                await asyncio.to_thread(lambda: list(network_client.load_balancers.list_all()))
                if include_load_balancers
                else []
            )
        except (ClientAuthenticationError, HttpResponseError) as exc:
            raise CloudConnectorError(str(exc)) from exc

        # Index NICs + public IPs by ARM id so VM / LB frontends resolve
        # without an extra round-trip per reference.
        nic_by_id = {nic.id: nic for nic in nics}
        pip_by_id = {pip.id: pip for pip in public_ips}

        self._collect_networks(vnets, inventory)
        self._collect_instances(
            vms,
            nic_by_id,
            pip_by_id,
            compute_client,
            inventory,
            include_stopped=include_stopped,
        )
        self._collect_public_ips(public_ips, inventory)
        if include_load_balancers:
            self._collect_load_balancers(lbs, pip_by_id, inventory)

    # ── Per-resource collectors ────────────────────────────────────────

    def _collect_networks(self, vnets: list[Any], inventory: CloudInventory) -> None:
        """Normalise VNets + their nested subnets into the inventory."""
        for vnet in vnets:
            if not self._in_region(vnet.location):
                continue
            address_prefixes = tuple(getattr(vnet.address_space, "address_prefixes", None) or ())
            inventory.networks.append(
                CloudNetwork(
                    id=vnet.id,
                    name=vnet.name,
                    cidrs=address_prefixes,
                    region=vnet.location,
                )
            )
            for subnet in getattr(vnet, "subnets", None) or ():
                cidr = subnet.address_prefix or next(
                    iter(getattr(subnet, "address_prefixes", None) or ()),
                    None,
                )
                if not cidr:
                    continue
                inventory.subnets.append(
                    CloudSubnet(
                        id=subnet.id,
                        name=subnet.name,
                        network_id=vnet.id,
                        cidr=cidr,
                        region=vnet.location,
                    )
                )

    def _collect_instances(
        self,
        vms: list[Any],
        nic_by_id: dict[Any, Any],
        pip_by_id: dict[Any, Any],
        compute_client: Any,
        inventory: CloudInventory,
        *,
        include_stopped: bool,
    ) -> None:
        """Normalise VMs into instances, resolving NIC IPs + power state."""
        for vm in vms:
            if not self._in_region(vm.location):
                continue
            resource_group = self._rg_from_id(vm.id)
            running = self._vm_running(compute_client, resource_group, vm.name)
            if not include_stopped and not running:
                continue
            nics = self._resolve_vm_nics(vm, nic_by_id, pip_by_id)
            inventory.instances.append(
                CloudInstance(
                    id=vm.id,
                    name=vm.name,
                    running=running,
                    nics=tuple(nics),
                    region=vm.location,
                )
            )

    def _vm_running(self, compute_client: Any, resource_group: str | None, name: str) -> bool:
        """Resolve a VM's power state via ``instance_view`` (best-effort).

        A missing resource group or an instance-view error is treated as
        "not running" rather than failing the whole sync; the running
        flag is advisory and the reconciler still mirrors the row. Any
        probe failure (HTTP error, transient network, SDK quirk) is
        caught — a per-VM power-state lookup must never abort the sweep.
        """
        if not resource_group:
            return False
        try:
            view = compute_client.virtual_machines.instance_view(resource_group, name)
        except Exception as exc:  # noqa: BLE001 — best-effort probe; any failure → "not running"
            logger.warning(
                "cloud.azure.instance_view_failed",
                vm=name,
                resource_group=resource_group,
                error=str(exc),
            )
            return False
        for status in getattr(view, "statuses", None) or ():
            code = getattr(status, "code", "") or ""
            if code.startswith("PowerState/"):
                return code == "PowerState/running"
        return False

    def _resolve_vm_nics(
        self,
        vm: Any,
        nic_by_id: dict[Any, Any],
        pip_by_id: dict[Any, Any],
    ) -> list[CloudNic]:
        """Map a VM's referenced NICs into :class:`CloudNic` rows."""
        nics: list[CloudNic] = []
        profile = getattr(vm, "network_profile", None)
        for nic_ref in getattr(profile, "network_interfaces", None) or ():
            nic = nic_by_id.get(getattr(nic_ref, "id", None))
            if nic is None:
                continue
            mac = getattr(nic, "mac_address", None)
            for ip_config in getattr(nic, "ip_configurations", None) or ():
                private_ip = getattr(ip_config, "private_ip_address", None)
                if not private_ip:
                    continue
                public_ip = self._resolve_public_ip(
                    getattr(ip_config, "public_ip_address", None), pip_by_id
                )
                nics.append(CloudNic(private_ip=private_ip, public_ip=public_ip, mac=mac))
        return nics

    @staticmethod
    def _resolve_public_ip(reference: Any, pip_by_id: dict[Any, Any]) -> str | None:
        """Resolve a public-IP reference (id-only or inline) to its address."""
        if reference is None:
            return None
        # Inline expansions already carry ``ip_address``; the common ARM
        # shape is an id-only reference we look up in the pre-built index.
        address = getattr(reference, "ip_address", None)
        if address:
            return address
        pip = pip_by_id.get(getattr(reference, "id", None))
        return getattr(pip, "ip_address", None) if pip is not None else None

    def _collect_public_ips(self, public_ips: list[Any], inventory: CloudInventory) -> None:
        """Normalise standalone public IP address resources."""
        for pip in public_ips:
            if not self._in_region(getattr(pip, "location", None)):
                continue
            address = getattr(pip, "ip_address", None)
            if not address:
                continue  # unassigned (dynamic, not yet allocated) — skip
            inventory.public_ips.append(
                CloudPublicIP(
                    address=address,
                    name=pip.name or "",
                    attached=bool(getattr(pip, "ip_configuration", None)),
                )
            )

    def _collect_load_balancers(
        self,
        lbs: list[Any],
        pip_by_id: dict[Any, Any],
        inventory: CloudInventory,
    ) -> None:
        """Normalise LB frontend IP configs into load-balancer rows."""
        for lb in lbs:
            if not self._in_region(lb.location):
                continue
            frontend_ips: list[str] = []
            for frontend in getattr(lb, "frontend_ip_configurations", None) or ():
                public_ip = self._resolve_public_ip(
                    getattr(frontend, "public_ip_address", None), pip_by_id
                )
                if public_ip:
                    frontend_ips.append(public_ip)
                    continue
                private_ip = getattr(frontend, "private_ip_address", None)
                if private_ip:
                    frontend_ips.append(private_ip)
            inventory.load_balancers.append(
                CloudLoadBalancer(
                    id=lb.id,
                    name=lb.name,
                    frontend_ips=tuple(frontend_ips),
                    region=lb.location,
                )
            )


__all__ = ["AzureConnector"]
