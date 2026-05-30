"""Tests for the GCP cloud connector.

The ``google-cloud-compute`` / ``google-auth`` SDKs are optional and not
installed in CI, and there is no GCP test account — so every test stubs
the per-client factory methods (``_networks_client`` etc.) and the
credential builder (``_credentials``). Each stub returns Mocks whose
``list`` / ``aggregated_list`` yield ``SimpleNamespace`` objects shaped
like the real protobuf responses (note the snake_case ``network_i_p`` /
``nat_i_p`` / ``i_p_address`` quirks). Everything stays offline and
deterministic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app.services.cloud.base import CloudConnectorError, CloudProbeResult
from app.services.cloud.gcp import GCPConnector, _basename, _region_from_zone

# ── Fixtures: fake GCP protobuf objects + aggregated_list pages ──────────

_KEY = json.dumps(
    {
        "type": "service_account",
        "project_id": "proj-1",
        "client_email": "svc@proj-1.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    }
)


def _network(id_: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=id_, name=name)


def _subnet(id_: int, name: str, network: str, cidr: str, region: str) -> SimpleNamespace:
    # ``network`` / ``region`` are full self-links in the real API.
    return SimpleNamespace(
        id=id_,
        name=name,
        network=f"https://www.googleapis.com/compute/v1/projects/p/global/networks/{network}",
        ip_cidr_range=cidr,
        region=f"https://www.googleapis.com/compute/v1/projects/p/regions/{region}",
    )


def _nic(network_ip: str, nat_ip: str | None) -> SimpleNamespace:
    access = [SimpleNamespace(nat_i_p=nat_ip)] if nat_ip else []
    return SimpleNamespace(network_i_p=network_ip, access_configs=access)


def _instance(
    id_: int, name: str, status: str, zone: str, nics: list[SimpleNamespace]
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        name=name,
        status=status,
        zone=f"https://www.googleapis.com/compute/v1/projects/p/zones/{zone}",
        network_interfaces=nics,
    )


def _address(name: str, address: str, address_type: str, users: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, address=address, address_type=address_type, users=users)


def _forwarding_rule(id_: int, name: str, ip: str, region: str | None) -> SimpleNamespace:
    region_link = (
        f"https://www.googleapis.com/compute/v1/projects/p/regions/{region}" if region else None
    )
    return SimpleNamespace(id=id_, name=name, i_p_address=ip, region=region_link)


def _agg(attr: str, scope: str, items: list[Any]) -> list[tuple[str, SimpleNamespace]]:
    """Build one ``aggregated_list`` page: a (scope, scoped_list) pair.

    The scoped list exposes the resource list under ``attr`` (the name the
    real client uses, e.g. ``subnetworks``). An empty scope carries only a
    ``warning`` attribute — modelled with the resource attr set to ``[]``.
    """
    scoped = SimpleNamespace(**{attr: items})
    return [(scope, scoped)]


def _make_connector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    networks: list[SimpleNamespace],
    subnets: list[SimpleNamespace],
    instances: list[SimpleNamespace],
    addresses: list[SimpleNamespace],
    global_addresses: list[SimpleNamespace],
    forwarding_rules: list[SimpleNamespace],
    global_forwarding_rules: list[SimpleNamespace],
    regions: list[str] | None = None,
    project_ids: list[str] | None = None,
) -> GCPConnector:
    conn = GCPConnector(
        credentials={"service_account_json": _KEY},
        provider_config={"project_ids": project_ids or ["proj-1"]},
        regions=regions,
    )

    # Skip the real SDK credential build entirely.
    monkeypatch.setattr(conn, "_credentials", lambda: object())

    def _client(list_items: list[Any] | None = None, agg_pages: Any = None) -> Mock:
        m = Mock()
        if list_items is not None:
            m.list.return_value = list_items
        if agg_pages is not None:
            m.aggregated_list.return_value = agg_pages
        return m

    monkeypatch.setattr(conn, "_networks_client", lambda _c: _client(list_items=networks))
    monkeypatch.setattr(
        conn,
        "_subnetworks_client",
        lambda _c: _client(agg_pages=_agg("subnetworks", "regions/us-central1", subnets)),
    )
    monkeypatch.setattr(
        conn,
        "_instances_client",
        lambda _c: _client(agg_pages=_agg("instances", "zones/us-central1-a", instances)),
    )
    monkeypatch.setattr(
        conn,
        "_addresses_client",
        lambda _c: _client(agg_pages=_agg("addresses", "regions/us-central1", addresses)),
    )
    monkeypatch.setattr(
        conn, "_global_addresses_client", lambda _c: _client(list_items=global_addresses)
    )
    monkeypatch.setattr(
        conn,
        "_forwarding_rules_client",
        lambda _c: _client(
            agg_pages=_agg("forwarding_rules", "regions/us-central1", forwarding_rules)
        ),
    )
    monkeypatch.setattr(
        conn,
        "_global_forwarding_rules_client",
        lambda _c: _client(list_items=global_forwarding_rules),
    )
    return conn


# ── Helper unit tests ────────────────────────────────────────────────────


def test_basename_handles_selflink_and_empty() -> None:
    assert _basename("https://x/projects/p/regions/us-central1") == "us-central1"
    assert _basename("default") == "default"
    assert _basename(None) == ""
    assert _basename("") == ""


def test_region_from_zone_trims_suffix() -> None:
    assert _region_from_zone("https://x/zones/us-central1-a") == "us-central1"
    assert _region_from_zone("europe-west4-b") == "europe-west4"
    assert _region_from_zone(None) == ""


# ── Inventory ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_inventory_full_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _make_connector(
        monkeypatch,
        networks=[_network(111, "vpc-a")],
        subnets=[_subnet(222, "sub-a", "vpc-a", "10.0.0.0/24", "us-central1")],
        instances=[
            _instance(
                333,
                "vm-running",
                "RUNNING",
                "us-central1-a",
                [_nic("10.0.0.5", "34.1.2.3")],
            ),
            _instance(444, "vm-stopped", "TERMINATED", "us-central1-a", [_nic("10.0.0.6", None)]),
        ],
        addresses=[
            _address("ext-ip", "34.9.9.9", "EXTERNAL", users=["fwd-rule"]),
            _address("int-ip", "10.0.0.250", "INTERNAL", users=[]),
        ],
        global_addresses=[],
        forwarding_rules=[_forwarding_rule(555, "lb-rule", "34.5.5.5", "us-central1")],
        global_forwarding_rules=[],
    )

    inv = await conn.fetch_inventory()

    assert inv.account_id == "svc@proj-1.iam.gserviceaccount.com"
    assert not inv.warnings

    # Network carries NO cidrs (GCP quirk) — CIDR lives on the subnet.
    assert len(inv.networks) == 1
    net = inv.networks[0]
    assert net.id == "111"
    assert net.name == "vpc-a"
    assert net.cidrs == ()

    # Subnet carries the CIDR + region and resolves network_id to the net id.
    assert len(inv.subnets) == 1
    sub = inv.subnets[0]
    assert sub.id == "222"
    assert sub.cidr == "10.0.0.0/24"
    assert sub.region == "us-central1"
    assert sub.network_id == net.id

    # include_stopped default False → only the RUNNING instance, with its NIC.
    assert len(inv.instances) == 1
    vm = inv.instances[0]
    assert vm.name == "vm-running"
    assert vm.running is True
    assert vm.region == "us-central1"
    assert len(vm.nics) == 1
    assert vm.nics[0].private_ip == "10.0.0.5"
    assert vm.nics[0].public_ip == "34.1.2.3"
    assert vm.nics[0].mac is None

    # Only the EXTERNAL address surfaces, attached because it has users.
    assert len(inv.public_ips) == 1
    pip = inv.public_ips[0]
    assert pip.address == "34.9.9.9"
    assert pip.attached is True

    # Forwarding rule → load balancer frontend.
    assert len(inv.load_balancers) == 1
    lb = inv.load_balancers[0]
    assert lb.id == "555"
    assert lb.frontend_ips == ("34.5.5.5",)
    assert lb.region == "us-central1"


@pytest.mark.asyncio
async def test_include_stopped_returns_terminated_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_connector(
        monkeypatch,
        networks=[_network(111, "vpc-a")],
        subnets=[],
        instances=[
            _instance(333, "vm-running", "RUNNING", "us-central1-a", [_nic("10.0.0.5", None)]),
            _instance(444, "vm-stopped", "TERMINATED", "us-central1-a", [_nic("10.0.0.6", None)]),
        ],
        addresses=[],
        global_addresses=[],
        forwarding_rules=[],
        global_forwarding_rules=[],
    )

    inv = await conn.fetch_inventory(include_stopped=True)

    names = {i.name: i.running for i in inv.instances}
    assert names == {"vm-running": True, "vm-stopped": False}


@pytest.mark.asyncio
async def test_subnet_unknown_network_warns_and_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _make_connector(
        monkeypatch,
        networks=[_network(111, "vpc-a")],
        subnets=[_subnet(222, "orphan", "vpc-MISSING", "10.0.0.0/24", "us-central1")],
        instances=[],
        addresses=[],
        global_addresses=[],
        forwarding_rules=[],
        global_forwarding_rules=[],
    )

    inv = await conn.fetch_inventory()

    assert inv.subnets == []
    assert any("unknown network" in w for w in inv.warnings)


@pytest.mark.asyncio
async def test_region_allowlist_filters_subnets_and_lbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_connector(
        monkeypatch,
        networks=[_network(111, "vpc-a")],
        subnets=[
            _subnet(222, "keep", "vpc-a", "10.0.0.0/24", "us-central1"),
            _subnet(223, "drop", "vpc-a", "10.1.0.0/24", "europe-west4"),
        ],
        instances=[],
        addresses=[],
        global_addresses=[],
        forwarding_rules=[
            _forwarding_rule(555, "keep-lb", "34.5.5.5", "us-central1"),
            _forwarding_rule(556, "drop-lb", "34.6.6.6", "europe-west4"),
        ],
        global_forwarding_rules=[_forwarding_rule(557, "global-lb", "34.7.7.7", None)],
        regions=["us-central1"],
    )

    inv = await conn.fetch_inventory()

    assert [s.name for s in inv.subnets] == ["keep"]
    # Regional LB outside the allow-list dropped; global LB always kept.
    lb_names = {lb.name for lb in inv.load_balancers}
    assert lb_names == {"keep-lb", "global-lb"}
    assert next(lb for lb in inv.load_balancers if lb.name == "global-lb").region == "global"


@pytest.mark.asyncio
async def test_include_load_balancers_false_skips_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_connector(
        monkeypatch,
        networks=[_network(111, "vpc-a")],
        subnets=[],
        instances=[],
        addresses=[],
        global_addresses=[],
        forwarding_rules=[_forwarding_rule(555, "lb-rule", "34.5.5.5", "us-central1")],
        global_forwarding_rules=[_forwarding_rule(557, "global-lb", "34.7.7.7", None)],
    )

    inv = await conn.fetch_inventory(include_load_balancers=False)

    assert inv.load_balancers == []


@pytest.mark.asyncio
async def test_per_project_failure_becomes_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = GCPConnector(
        credentials={"service_account_json": _KEY},
        provider_config={"project_ids": ["proj-1"]},
    )
    monkeypatch.setattr(conn, "_credentials", lambda: object())
    monkeypatch.setattr(conn, "_describe_error", staticmethod(lambda exc: f"boom: {exc}"))

    def _boom(_c: Any) -> Mock:
        m = Mock()
        m.list.side_effect = RuntimeError("api down")
        return m

    monkeypatch.setattr(conn, "_networks_client", _boom)

    inv = await conn.fetch_inventory()

    assert inv.networks == []
    assert any("proj-1" in w and "boom" in w for w in inv.warnings)


@pytest.mark.asyncio
async def test_fetch_inventory_no_projects_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = GCPConnector(
        credentials={"service_account_json": _KEY},
        provider_config={"project_ids": []},
    )
    monkeypatch.setattr(conn, "_credentials", lambda: object())

    inv = await conn.fetch_inventory()

    assert inv.networks == []
    assert any("no project_ids" in w for w in inv.warnings)


@pytest.mark.asyncio
async def test_fetch_inventory_bad_credentials_raises() -> None:
    conn = GCPConnector(
        credentials={"service_account_json": "not-json"},
        provider_config={"project_ids": ["proj-1"]},
    )
    with pytest.raises(CloudConnectorError):
        await conn.fetch_inventory()


# ── Probe ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _make_connector(
        monkeypatch,
        networks=[_network(111, "vpc-a"), _network(112, "vpc-b")],
        subnets=[],
        instances=[],
        addresses=[],
        global_addresses=[],
        forwarding_rules=[],
        global_forwarding_rules=[],
    )

    result = await conn.probe()

    assert isinstance(result, CloudProbeResult)
    assert result.ok is True
    assert result.account_id == "svc@proj-1.iam.gserviceaccount.com"
    assert result.network_count == 2


@pytest.mark.asyncio
async def test_probe_auth_failure_returns_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = GCPConnector(
        credentials={"service_account_json": _KEY},
        provider_config={"project_ids": ["proj-1"]},
    )
    monkeypatch.setattr(conn, "_credentials", lambda: object())
    monkeypatch.setattr(conn, "_describe_error", staticmethod(lambda exc: "GCP auth failed"))

    def _boom(_c: Any) -> Mock:
        m = Mock()
        m.list.side_effect = RuntimeError("403 forbidden")
        return m

    monkeypatch.setattr(conn, "_networks_client", _boom)

    result = await conn.probe()

    assert result.ok is False
    assert "GCP auth failed" in result.message
    # account_id still derived from the parsed key even on failure.
    assert result.account_id == "svc@proj-1.iam.gserviceaccount.com"


@pytest.mark.asyncio
async def test_probe_bad_credentials_returns_not_ok() -> None:
    conn = GCPConnector(
        credentials={"service_account_json": "not-json"},
        provider_config={"project_ids": ["proj-1"]},
    )

    result = await conn.probe()

    assert result.ok is False
    assert "JSON" in result.message or "valid" in result.message.lower()


@pytest.mark.asyncio
async def test_probe_no_projects_returns_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = GCPConnector(
        credentials={"service_account_json": _KEY},
        provider_config={"project_ids": []},
    )
    monkeypatch.setattr(conn, "_credentials", lambda: object())

    result = await conn.probe()

    assert result.ok is False
    assert "project_ids" in result.message
