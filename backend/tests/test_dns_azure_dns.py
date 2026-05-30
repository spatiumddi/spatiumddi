"""Unit tests for the Azure DNS cloud driver (issue #37, Part B).

These are tier-3 provider tests — no Azure account is available, so the
``azure-mgmt-dns`` SDK is never imported and never hit. Every test
monkeypatches :meth:`AzureDNSDriver._client` to return a ``Mock`` whose
``zones`` / ``record_sets`` namespaces yield ``SimpleNamespace`` objects
shaped exactly like the ``azure-mgmt-dns`` models. Fully offline +
deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app.drivers.dns._cloud_base import CloudDNSError, CloudDNSZone
from app.drivers.dns.azuredns import AzureDNSDriver
from app.drivers.dns.base import RecordChange, RecordData

# ── Fixtures ───────────────────────────────────────────────────────────────

_CREDS = {
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "s",
    "subscription_id": "sub",
    "resource_group": "rg",
}


class _FakeHttpResponseError(Exception):
    """Stand-in for ``azure.core.exceptions.HttpResponseError``.

    The driver's ``_wrap_errors`` lazy-imports the real exception types;
    when those imports fail in the test env it falls back to wrapping any
    exception as a ``CloudDNSError`` anyway, so a plain subclass is enough
    to exercise the error path deterministically.
    """


@pytest.fixture
def server() -> SimpleNamespace:
    return SimpleNamespace(id="srv-1", name="azure-1", credentials_encrypted=b"blob")


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> AzureDNSDriver:
    """Driver with credential decrypt stubbed (no Fernet key in tests)."""
    drv = AzureDNSDriver()
    monkeypatch.setattr(drv, "_load_credentials", lambda srv: dict(_CREDS))
    return drv


def _patch_client(monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, client: Any) -> None:
    monkeypatch.setattr(driver, "_client", lambda creds: client)


# ── Registry / capabilities ────────────────────────────────────────────────


def test_name_and_credential_fields() -> None:
    drv = AzureDNSDriver()
    assert drv.name == "azure_dns"
    assert drv.credential_fields == (
        "tenant_id",
        "client_id",
        "client_secret",
        "subscription_id",
        "resource_group",
    )


def test_capabilities_shape() -> None:
    caps = AzureDNSDriver().capabilities()
    assert caps["name"] == "azure_dns"
    assert caps["agentless"] is True
    assert caps["manages_zones"] is True
    assert caps["alias_records"] is True
    assert caps["dnssec_online"] is False
    assert "SOA" in caps["record_types"]
    assert "online DNSSEC" in caps["notes"]


# ── Zone listing ────────────────────────────────────────────────────────────


async def test_list_zones_by_resource_group(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    client.zones.list_by_resource_group.return_value = [
        SimpleNamespace(name="example.com", number_of_record_sets=12),
        SimpleNamespace(name="0.0.10.in-addr.arpa", number_of_record_sets=3),
    ]
    _patch_client(monkeypatch, driver, client)

    zones = await driver._list_zones(server, dict(_CREDS))

    client.zones.list_by_resource_group.assert_called_once_with("rg")
    assert zones == [
        CloudDNSZone(name="example.com.", zone_id="example.com", is_reverse=False, record_count=12),
        CloudDNSZone(
            name="0.0.10.in-addr.arpa.",
            zone_id="0.0.10.in-addr.arpa",
            is_reverse=True,
            record_count=3,
        ),
    ]


async def test_list_zones_falls_back_to_subscription_list_when_rg_empty(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    client.zones.list.return_value = [SimpleNamespace(name="example.org", number_of_record_sets=1)]
    _patch_client(monkeypatch, driver, client)

    creds = dict(_CREDS, resource_group="")
    zones = await driver._list_zones(server, creds)

    client.zones.list.assert_called_once_with()
    client.zones.list_by_resource_group.assert_not_called()
    assert [z.name for z in zones] == ["example.org."]


async def test_pull_zones_from_server_returns_neutral_dicts(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    client.zones.list_by_resource_group.return_value = [
        SimpleNamespace(name="example.com", number_of_record_sets=7)
    ]
    _patch_client(monkeypatch, driver, client)

    rows = await driver.pull_zones_from_server(server)

    assert rows == [
        {
            "name": "example.com.",
            "zone_type": "Primary",
            "is_reverse_lookup": False,
            "dnssec_enabled": False,
            "zone_id": "example.com",
            "record_count": 7,
        }
    ]


# ── Record listing / multi-type expansion ───────────────────────────────────


async def test_list_zone_records_expands_multiple_types(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    record_sets = [
        # A record set with two records.
        SimpleNamespace(
            name="www",
            type="Microsoft.Network/dnszones/A",
            ttl=300,
            a_records=[
                SimpleNamespace(ipv4_address="10.0.0.1"),
                SimpleNamespace(ipv4_address="10.0.0.2"),
            ],
        ),
        # MX record set.
        SimpleNamespace(
            name="@",
            type="Microsoft.Network/dnszones/MX",
            ttl=3600,
            mx_records=[SimpleNamespace(preference=10, exchange="mail.example.com")],
        ),
        # TXT record set (Azure splits long strings into a value list).
        SimpleNamespace(
            name="@",
            type="Microsoft.Network/dnszones/TXT",
            ttl=3600,
            txt_records=[SimpleNamespace(value=["v=spf1 ", "-all"])],
        ),
        # SOA is skipped entirely.
        SimpleNamespace(name="@", type="Microsoft.Network/dnszones/SOA", ttl=3600),
    ]
    client = Mock()
    client.record_sets.list_by_dns_zone.return_value = record_sets
    _patch_client(monkeypatch, driver, client)

    records = await driver._list_zone_records(server, dict(_CREDS), "example.com.")

    # Azure zone label is stripped of the trailing dot for the SDK call.
    client.record_sets.list_by_dns_zone.assert_called_once_with("rg", "example.com")

    assert records == [
        RecordData(name="www", record_type="A", value="10.0.0.1", ttl=300),
        RecordData(name="www", record_type="A", value="10.0.0.2", ttl=300),
        RecordData(name="@", record_type="MX", value="10 mail.example.com", ttl=3600),
        RecordData(name="@", record_type="TXT", value="v=spf1 -all", ttl=3600),
    ]


async def test_list_zone_records_expands_srv_and_caa(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    record_sets = [
        SimpleNamespace(
            name="_sip._tcp",
            type="Microsoft.Network/dnszones/SRV",
            ttl=3600,
            srv_records=[
                SimpleNamespace(priority=10, weight=20, port=5060, target="sip.example.com")
            ],
        ),
        SimpleNamespace(
            name="@",
            type="Microsoft.Network/dnszones/CAA",
            ttl=3600,
            caa_records=[SimpleNamespace(flags=0, tag="issue", value="letsencrypt.org")],
        ),
    ]
    client = Mock()
    client.record_sets.list_by_dns_zone.return_value = record_sets
    _patch_client(monkeypatch, driver, client)

    records = await driver._list_zone_records(server, dict(_CREDS), "example.com.")

    assert records[0] == RecordData(
        name="_sip._tcp", record_type="SRV", value="10 20 5060 sip.example.com", ttl=3600
    )
    assert records[1] == RecordData(
        name="@", record_type="CAA", value="0 issue letsencrypt.org", ttl=3600
    )


# ── Record write (create_or_update param building) ──────────────────────────


async def test_apply_record_create_builds_a_params(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="10.0.0.5", ttl=120),
        target_serial=1,
    )
    await driver._apply_record(server, dict(_CREDS), change)

    client.record_sets.create_or_update.assert_called_once_with(
        "rg", "example.com", "www", "A", {"ttl": 120, "a_records": [{"ipv4_address": "10.0.0.5"}]}
    )


async def test_apply_record_update_builds_mx_params(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="update",
        zone_name="example.com.",
        record=RecordData(name="@", record_type="MX", value="20 mail2.example.com", ttl=3600),
        target_serial=2,
    )
    await driver._apply_record(server, dict(_CREDS), change)

    _, _, _, rtype, params = client.record_sets.create_or_update.call_args.args
    assert rtype == "MX"
    assert params == {
        "ttl": 3600,
        "mx_records": [{"preference": 20, "exchange": "mail2.example.com"}],
    }


async def test_apply_record_builds_srv_params(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(
            name="_sip._tcp", record_type="SRV", value="10 20 5060 sip.example.com", ttl=3600
        ),
        target_serial=3,
    )
    await driver._apply_record(server, dict(_CREDS), change)

    _, _, _, _, params = client.record_sets.create_or_update.call_args.args
    assert params["srv_records"] == [
        {"priority": 10, "weight": 20, "port": 5060, "target": "sip.example.com"}
    ]


async def test_apply_record_delete_dispatches_to_delete(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="old", record_type="A", value="10.0.0.9"),
        target_serial=4,
    )
    await driver._apply_record(server, dict(_CREDS), change)

    client.record_sets.delete.assert_called_once_with("rg", "example.com", "old", "A")
    client.record_sets.create_or_update.assert_not_called()


# ── Zone write ───────────────────────────────────────────────────────────────


async def test_apply_zone_create(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    _patch_client(monkeypatch, driver, client)

    zone = SimpleNamespace(name="new.example.com.")
    await driver._apply_zone(server, dict(_CREDS), zone, "create")

    client.zones.create_or_update.assert_called_once_with(
        "rg", "new.example.com", {"location": "global"}
    )


async def test_apply_zone_delete_uses_lro_poller(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    poller = Mock()
    client = Mock()
    client.zones.begin_delete.return_value = poller
    _patch_client(monkeypatch, driver, client)

    zone = SimpleNamespace(name="gone.example.com.")
    await driver._apply_zone(server, dict(_CREDS), zone, "delete")

    client.zones.begin_delete.assert_called_once_with("rg", "gone.example.com")
    poller.result.assert_called_once_with()


# ── Error wrapping ───────────────────────────────────────────────────────────


async def test_list_zones_wraps_http_error_as_cloud_dns_error(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    client.zones.list_by_resource_group.side_effect = _FakeHttpResponseError("403 Forbidden")
    _patch_client(monkeypatch, driver, client)

    with pytest.raises(CloudDNSError) as excinfo:
        await driver._list_zones(server, dict(_CREDS))
    assert "403 Forbidden" in str(excinfo.value)


async def test_apply_record_wraps_error_as_cloud_dns_error(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    client.record_sets.create_or_update.side_effect = _FakeHttpResponseError("boom")
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="10.0.0.1", ttl=300),
        target_serial=5,
    )
    with pytest.raises(CloudDNSError):
        await driver._apply_record(server, dict(_CREDS), change)


# ── Probe (inherited from base, exercised end-to-end with mocked client) ─────


async def test_probe_ok_reports_zone_count(
    monkeypatch: pytest.MonkeyPatch, driver: AzureDNSDriver, server: SimpleNamespace
) -> None:
    client = Mock()
    client.zones.list_by_resource_group.return_value = [
        SimpleNamespace(name="example.com", number_of_record_sets=1)
    ]
    _patch_client(monkeypatch, driver, client)

    probe = await driver.probe(server)
    assert probe.ok is True
    assert probe.zone_count == 1
