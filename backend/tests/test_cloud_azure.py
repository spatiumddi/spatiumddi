"""Offline unit tests for the Azure cloud connector (#37).

These are tier-3 provider tests with no Azure account available, so they
never touch a real API. The Azure SDK isn't a runtime dependency here, so
we inject minimal stub ``azure.*`` modules into ``sys.modules`` (giving the
connector's lazy ``from azure.core.exceptions import ...`` a real exception
class to catch) and monkeypatch the connector's client-factory layer
(``_credential`` / ``_network_client`` / ``_compute_client``) to return
SimpleNamespace stubs shaped like the ARM SDK responses.

Coverage:
  * ``fetch_inventory`` normalises a VNet with 2 address prefixes + a
    nested subnet, a running VM with one NIC (private + public + MAC),
    a standalone public IP, and an LB frontend.
  * region allow-list filters resources by location.
  * ``include_stopped`` gates non-running VMs.
  * a per-subscription HttpResponseError folds into ``warnings`` while a
    healthy subscription still reconciles; an all-subscriptions failure
    raises ``CloudConnectorError``.
  * ``probe`` ok path + ``ClientAuthenticationError`` failure path.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

# ── Stub azure.* modules so the connector's lazy imports resolve ───────
#
# The connector only ever imports two exception classes from the SDK
# (``ClientAuthenticationError`` + ``HttpResponseError``); the client
# classes themselves are reached through the monkeypatched factory layer,
# so the stub packages only need to exist (empty) to satisfy the import
# machinery. Installed at import time, before the connector is imported.


class _StubHttpResponseError(Exception):
    """Stand-in for ``azure.core.exceptions.HttpResponseError``."""


class _StubClientAuthenticationError(_StubHttpResponseError):
    """Stand-in for ``azure.core.exceptions.ClientAuthenticationError``."""


def _install_azure_stubs() -> None:
    core = types.ModuleType("azure.core")
    core_exc = types.ModuleType("azure.core.exceptions")
    core_exc.HttpResponseError = _StubHttpResponseError  # type: ignore[attr-defined]
    core_exc.ClientAuthenticationError = _StubClientAuthenticationError  # type: ignore[attr-defined]

    azure = types.ModuleType("azure")
    identity = types.ModuleType("azure.identity")
    mgmt = types.ModuleType("azure.mgmt")
    mgmt_network = types.ModuleType("azure.mgmt.network")
    mgmt_compute = types.ModuleType("azure.mgmt.compute")
    # The factory layer is monkeypatched in tests, so these only need to
    # be importable; give them no-op placeholders.
    identity.ClientSecretCredential = object  # type: ignore[attr-defined]
    mgmt_network.NetworkManagementClient = object  # type: ignore[attr-defined]
    mgmt_compute.ComputeManagementClient = object  # type: ignore[attr-defined]

    sys.modules.setdefault("azure", azure)
    sys.modules.setdefault("azure.core", core)
    sys.modules["azure.core.exceptions"] = core_exc
    sys.modules["azure.identity"] = identity
    sys.modules["azure.mgmt"] = mgmt
    sys.modules["azure.mgmt.network"] = mgmt_network
    sys.modules["azure.mgmt.compute"] = mgmt_compute


_install_azure_stubs()

from app.services.cloud.azure import AzureConnector  # noqa: E402
from app.services.cloud.base import (  # noqa: E402
    CloudConnectorError,
    CloudInventory,
    CloudProbeResult,
)

_CREDS = {
    "tenant_id": "tenant-1",
    "client_id": "client-1",
    "client_secret": "secret-1",
}

# ARM resource ids are long; build them from a shared scope prefix so every
# literal stays under the 100-char line limit and the shapes read clearly.
_NET = "/subscriptions/sub-1/resourceGroups/rg1/providers/Microsoft.Network"
_COMPUTE = "/subscriptions/sub-1/resourceGroups/rg1/providers/Microsoft.Compute"
_VNET_ID = f"{_NET}/virtualNetworks/vnet1"
_SUBNET_ID = f"{_VNET_ID}/subnets/snet1"
_PIP_ID = f"{_NET}/publicIPAddresses/pip1"
_NIC_ID = f"{_NET}/networkInterfaces/nic1"
_LB_ID = f"{_NET}/loadBalancers/lb1"
_VM_ID = f"{_COMPUTE}/virtualMachines/vm1"


# ── ARM-shaped stub builders ───────────────────────────────────────────


def _vnet() -> SimpleNamespace:
    return SimpleNamespace(
        id=_VNET_ID,
        name="vnet1",
        location="eastus",
        address_space=SimpleNamespace(address_prefixes=["10.0.0.0/16", "10.1.0.0/16"]),
        subnets=[
            SimpleNamespace(
                id=_SUBNET_ID,
                name="snet1",
                address_prefix="10.0.1.0/24",
                address_prefixes=None,
            )
        ],
    )


def _public_ip() -> SimpleNamespace:
    return SimpleNamespace(
        id=_PIP_ID,
        name="pip1",
        location="eastus",
        ip_address="52.1.2.3",
        ip_configuration=SimpleNamespace(id="ipconf-1"),
    )


def _nic() -> SimpleNamespace:
    return SimpleNamespace(
        id=_NIC_ID,
        mac_address="00-0D-3A-11-22-33",
        ip_configurations=[
            SimpleNamespace(
                private_ip_address="10.0.1.4",
                public_ip_address=SimpleNamespace(id=_PIP_ID, ip_address=None),
            )
        ],
    )


def _vm() -> SimpleNamespace:
    return SimpleNamespace(
        id=_VM_ID,
        name="vm1",
        location="eastus",
        network_profile=SimpleNamespace(network_interfaces=[SimpleNamespace(id=_NIC_ID)]),
    )


def _lb() -> SimpleNamespace:
    return SimpleNamespace(
        id=_LB_ID,
        name="lb1",
        location="eastus",
        frontend_ip_configurations=[
            SimpleNamespace(
                public_ip_address=SimpleNamespace(id=_PIP_ID, ip_address=None),
                private_ip_address=None,
            )
        ],
    )


class _StubNetworkClient:
    def __init__(self, *, raise_on_list: Exception | None = None) -> None:
        self._raise = raise_on_list
        self.virtual_networks = SimpleNamespace(list_all=self._vnets)
        self.network_interfaces = SimpleNamespace(list_all=lambda: [_nic()])
        self.public_ip_addresses = SimpleNamespace(list_all=lambda: [_public_ip()])
        self.load_balancers = SimpleNamespace(list_all=lambda: [_lb()])

    def _vnets(self) -> list[SimpleNamespace]:
        if self._raise is not None:
            raise self._raise
        return [_vnet()]


class _StubComputeClient:
    def __init__(self, *, running: bool = True) -> None:
        self._running = running
        self.virtual_machines = SimpleNamespace(
            list_all=lambda: [_vm()],
            instance_view=self._instance_view,
        )

    def _instance_view(self, resource_group: str, name: str) -> SimpleNamespace:
        code = "PowerState/running" if self._running else "PowerState/deallocated"
        return SimpleNamespace(
            statuses=[
                SimpleNamespace(code="ProvisioningState/succeeded"),
                SimpleNamespace(code=code),
            ]
        )


def _make_connector(
    *,
    regions: list[str] | None = None,
    network_client: Any = None,
    compute_client: Any = None,
    network_factory: Any = None,
) -> AzureConnector:
    """Build a connector with monkeypatched factories.

    ``network_factory`` takes precedence (per-subscription dispatch);
    otherwise a fixed ``network_client`` / ``compute_client`` is returned
    for every subscription.
    """
    conn = AzureConnector(
        credentials=_CREDS,
        provider_config={"subscription_ids": ["sub-1"]},
        regions=regions,
    )
    net = network_client or _StubNetworkClient()
    comp = compute_client or _StubComputeClient()
    conn._credential = lambda: SimpleNamespace()  # type: ignore[method-assign]
    if network_factory is not None:
        conn._network_client = network_factory  # type: ignore[method-assign]
    else:
        conn._network_client = lambda subscription_id: net  # type: ignore[method-assign]
    conn._compute_client = lambda subscription_id: comp  # type: ignore[method-assign]
    return conn


# ── Tests ──────────────────────────────────────────────────────────────


async def test_fetch_inventory_normalizes_all_resource_kinds() -> None:
    conn = _make_connector()
    inv = await conn.fetch_inventory()

    assert isinstance(inv, CloudInventory)
    assert inv.account_id == "sub-1"
    assert inv.warnings == []

    # VNet with two address prefixes → one CloudNetwork carrying both.
    assert len(inv.networks) == 1
    net = inv.networks[0]
    assert net.name == "vnet1"
    assert net.cidrs == ("10.0.0.0/16", "10.1.0.0/16")
    assert net.region == "eastus"

    # Nested subnet normalised with parent network_id linkage.
    assert len(inv.subnets) == 1
    snet = inv.subnets[0]
    assert snet.name == "snet1"
    assert snet.cidr == "10.0.1.0/24"
    assert snet.network_id == net.id

    # Running VM with a single NIC: private + resolved public + MAC.
    assert len(inv.instances) == 1
    vm = inv.instances[0]
    assert vm.name == "vm1"
    assert vm.running is True
    assert len(vm.nics) == 1
    nic = vm.nics[0]
    assert nic.private_ip == "10.0.1.4"
    assert nic.public_ip == "52.1.2.3"  # resolved via pip index, not inline
    assert nic.mac == "00-0D-3A-11-22-33"

    # Standalone public IP.
    assert len(inv.public_ips) == 1
    pip = inv.public_ips[0]
    assert pip.address == "52.1.2.3"
    assert pip.name == "pip1"
    assert pip.attached is True

    # LB frontend resolved to the public IP.
    assert len(inv.load_balancers) == 1
    lb = inv.load_balancers[0]
    assert lb.name == "lb1"
    assert lb.frontend_ips == ("52.1.2.3",)


async def test_region_allowlist_filters_out_non_matching() -> None:
    conn = _make_connector(regions=["westeurope"])
    inv = await conn.fetch_inventory()
    # Every stub resource lives in eastus, so all are filtered out.
    assert inv.networks == []
    assert inv.subnets == []
    assert inv.instances == []
    assert inv.public_ips == []
    assert inv.load_balancers == []


async def test_include_stopped_gates_non_running_vms() -> None:
    stopped = _make_connector(compute_client=_StubComputeClient(running=False))
    inv = await stopped.fetch_inventory(include_stopped=False)
    assert inv.instances == []

    stopped2 = _make_connector(compute_client=_StubComputeClient(running=False))
    inv2 = await stopped2.fetch_inventory(include_stopped=True)
    assert len(inv2.instances) == 1
    assert inv2.instances[0].running is False


async def test_include_load_balancers_false_skips_lbs() -> None:
    conn = _make_connector()
    inv = await conn.fetch_inventory(include_load_balancers=False)
    assert inv.load_balancers == []
    # Other resources still present.
    assert len(inv.networks) == 1


async def test_per_subscription_failure_folds_into_warnings() -> None:
    healthy = _StubNetworkClient()
    broken = _StubNetworkClient(raise_on_list=_StubHttpResponseError("boom"))

    def factory(subscription_id: str) -> _StubNetworkClient:
        return healthy if subscription_id == "sub-1" else broken

    conn = AzureConnector(
        credentials=_CREDS,
        provider_config={"subscription_ids": ["sub-1", "sub-2"]},
        regions=None,
    )
    conn._credential = lambda: SimpleNamespace()  # type: ignore[method-assign]
    conn._network_client = factory  # type: ignore[method-assign]
    conn._compute_client = lambda subscription_id: _StubComputeClient()  # type: ignore[method-assign]

    inv = await conn.fetch_inventory()
    # The healthy subscription still produced its VNet.
    assert len(inv.networks) == 1
    # The broken one is recorded as a non-fatal warning.
    assert any("sub-2" in w and "boom" in w for w in inv.warnings)


async def test_all_subscriptions_failing_raises() -> None:
    broken = _StubNetworkClient(raise_on_list=_StubHttpResponseError("dead"))
    conn = _make_connector(network_client=broken)
    with pytest.raises(CloudConnectorError):
        await conn.fetch_inventory()


async def test_no_subscriptions_raises() -> None:
    conn = AzureConnector(
        credentials=_CREDS,
        provider_config={"subscription_ids": []},
        regions=None,
    )
    with pytest.raises(CloudConnectorError):
        await conn.fetch_inventory()


async def test_probe_ok() -> None:
    conn = _make_connector()
    result = await conn.probe()
    assert isinstance(result, CloudProbeResult)
    assert result.ok is True
    assert result.account_id == "sub-1"
    assert result.network_count == 1


async def test_probe_auth_failure_returns_not_ok() -> None:
    broken = _StubNetworkClient(raise_on_list=_StubClientAuthenticationError("bad creds"))
    conn = _make_connector(network_client=broken)
    result = await conn.probe()
    assert result.ok is False
    assert "bad creds" in result.message
    assert result.account_id == "sub-1"


async def test_probe_no_subscriptions_returns_not_ok() -> None:
    conn = AzureConnector(
        credentials=_CREDS,
        provider_config={"subscription_ids": []},
        regions=None,
    )
    result = await conn.probe()
    assert result.ok is False
    # Falls back to tenant_id for the account_id when no subscription set.
    assert result.account_id == "tenant-1"


def test_rg_from_id_extracts_resource_group() -> None:
    rid = (
        "/subscriptions/sub-1/resourceGroups/MyRG/providers/"
        "Microsoft.Compute/virtualMachines/vm1"
    )
    assert AzureConnector._rg_from_id(rid) == "MyRG"
    assert AzureConnector._rg_from_id(None) is None
    assert AzureConnector._rg_from_id("/subscriptions/sub-1") is None
