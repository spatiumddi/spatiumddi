"""Unit tests for the Amazon Route 53 cloud DNS driver (issue #37).

All tests are offline: the boto3 client factory (``_client``) is
monkeypatched to a ``Mock`` returning canned Route 53 API dicts, so
nothing ever touches AWS.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.drivers.dns._cloud_base import CloudDNSError
from app.drivers.dns.base import RecordChange, RecordData
from app.drivers.dns.route53 import Route53DNSDriver


def _server() -> SimpleNamespace:
    """A stand-in DNS server row (the driver only touches a few attrs)."""
    return SimpleNamespace(id="srv-1", name="aws", credentials_encrypted=None)


CREDS = {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "secret"}


def _patch_client(monkeypatch: pytest.MonkeyPatch, driver: Route53DNSDriver, client: Any) -> None:
    """Wire ``driver._client`` to return ``client`` regardless of creds."""
    monkeypatch.setattr(driver, "_client", lambda creds: client)


class _ClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError (offline)."""

    def __init__(self, code: str, message: str = "boom") -> None:
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


# ── Zone listing ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_zones_paginated_and_reverse(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones.side_effect = [
        {
            "HostedZones": [
                {"Id": "/hostedzone/Z1", "Name": "example.com.", "ResourceRecordSetCount": 5},
            ],
            "IsTruncated": True,
            "NextMarker": "m2",
        },
        {
            "HostedZones": [
                {
                    "Id": "/hostedzone/Z2",
                    "Name": "10.in-addr.arpa.",
                    "ResourceRecordSetCount": 2,
                },
            ],
            "IsTruncated": False,
        },
    ]
    _patch_client(monkeypatch, driver, client)

    zones = await driver._list_zones(_server(), CREDS)

    assert [z.name for z in zones] == ["example.com.", "10.in-addr.arpa."]
    assert [z.zone_id for z in zones] == ["Z1", "Z2"]
    assert [z.is_reverse for z in zones] == [False, True]
    assert [z.record_count for z in zones] == [5, 2]
    # Second page must be requested with the marker from the first page.
    assert client.list_hosted_zones.call_count == 2
    assert client.list_hosted_zones.call_args_list[1].kwargs["Marker"] == "m2"


@pytest.mark.asyncio
async def test_pull_zones_from_server_neutral_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones.return_value = {
        "HostedZones": [
            {"Id": "/hostedzone/Z1", "Name": "Example.COM.", "ResourceRecordSetCount": 3},
        ],
        "IsTruncated": False,
    }
    monkeypatch.setattr(_server(), "credentials_encrypted", b"x", raising=False)
    _patch_client(monkeypatch, driver, client)
    # Bypass credential decrypt — the base loads from credentials_encrypted.
    monkeypatch.setattr(driver, "_load_credentials", lambda server: CREDS)

    out = await driver.pull_zones_from_server(_server())

    assert out == [
        {
            "name": "example.com.",
            "zone_type": "Primary",
            "is_reverse_lookup": False,
            "dnssec_enabled": False,
            "zone_id": "Z1",
            "record_count": 3,
        }
    ]


# ── Record listing / relativization / multi-value expansion ───────────────


@pytest.mark.asyncio
async def test_list_zone_records_relativize_and_expand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.com."}]
    }
    client.list_resource_record_sets.side_effect = [
        {
            "ResourceRecordSets": [
                {
                    "Name": "example.com.",
                    "Type": "NS",
                    "TTL": 172800,
                    "ResourceRecords": [
                        {"Value": "ns-1.awsdns.com."},
                        {"Value": "ns-2.awsdns.net."},
                    ],
                },
                {
                    "Name": "www.example.com.",
                    "Type": "A",
                    "TTL": 300,
                    "ResourceRecords": [{"Value": "10.0.0.1"}],
                },
            ],
            "IsTruncated": True,
            "NextRecordName": "www.example.com.",
            "NextRecordType": "MX",
        },
        {
            "ResourceRecordSets": [
                {
                    "Name": "example.com.",
                    "Type": "MX",
                    "TTL": 3600,
                    "ResourceRecords": [{"Value": "10 mail.example.com."}],
                },
                {
                    "Name": "app.example.com.",
                    "Type": "A",
                    "AliasTarget": {"DNSName": "elb-123.us-east-1.elb.amazonaws.com."},
                },
            ],
            "IsTruncated": False,
        },
    ]
    _patch_client(monkeypatch, driver, client)

    records = await driver._list_zone_records(_server(), CREDS, "example.com.")

    # NS apex rrset expands into two RecordData rows (one per value),
    # relativized to "@".
    ns = [r for r in records if r.record_type == "NS"]
    assert len(ns) == 2
    assert {r.value for r in ns} == {"ns-1.awsdns.com.", "ns-2.awsdns.net."}
    assert all(r.name == "@" for r in ns)

    www = next(r for r in records if r.record_type == "A" and r.value == "10.0.0.1")
    assert www.name == "www"
    assert www.ttl == 300

    # MX keeps the priority baked into the value; priority stays None.
    mx = next(r for r in records if r.record_type == "MX")
    assert mx.value == "10 mail.example.com."
    assert mx.priority is None
    assert mx.name == "@"

    # ALIAS rrset → value is the target DNS name, ttl is None.
    alias = next(r for r in records if r.name == "app")
    assert alias.value == "elb-123.us-east-1.elb.amazonaws.com."
    assert alias.ttl is None

    # Pagination: the second call carries the start markers.
    assert client.list_resource_record_sets.call_count == 2
    second = client.list_resource_record_sets.call_args_list[1].kwargs
    assert second["StartRecordName"] == "www.example.com."
    assert second["StartRecordType"] == "MX"


@pytest.mark.asyncio
async def test_list_zone_records_zone_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    # API returns a neighbouring zone, not the one we asked for.
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z9", "Name": "other.com."}]
    }
    _patch_client(monkeypatch, driver, client)

    with pytest.raises(CloudDNSError, match="not found"):
        await driver._list_zone_records(_server(), CREDS, "example.com.")


# ── Record write: UPSERT vs DELETE change batches ─────────────────────────


@pytest.mark.asyncio
async def test_apply_record_upsert_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.com."}]
    }
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="10.0.0.1", ttl=120),
        target_serial=1,
    )
    await driver._apply_record(_server(), CREDS, change)

    kwargs = client.change_resource_record_sets.call_args.kwargs
    assert kwargs["HostedZoneId"] == "Z1"
    batch_change = kwargs["ChangeBatch"]["Changes"][0]
    assert batch_change["Action"] == "UPSERT"
    rrset = batch_change["ResourceRecordSet"]
    assert rrset == {
        "Name": "www.example.com.",
        "Type": "A",
        "TTL": 120,
        "ResourceRecords": [{"Value": "10.0.0.1"}],
    }


@pytest.mark.asyncio
async def test_apply_record_apex_and_default_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.com."}]
    }
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="update",
        zone_name="example.com.",
        record=RecordData(name="@", record_type="TXT", value="v=spf1 -all", ttl=None),
        target_serial=1,
    )
    await driver._apply_record(_server(), CREDS, change)

    rrset = client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0][
        "ResourceRecordSet"
    ]
    assert rrset["Name"] == "example.com."  # apex absolutized
    assert rrset["TTL"] == 300  # default when ttl is None


@pytest.mark.asyncio
async def test_apply_record_delete_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.com."}]
    }
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="old", record_type="A", value="10.0.0.9", ttl=300),
        target_serial=1,
    )
    await driver._apply_record(_server(), CREDS, change)

    batch_change = client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
    assert batch_change["Action"] == "DELETE"
    # DELETE must reconstruct the exact rrset (TTL + value).
    assert batch_change["ResourceRecordSet"] == {
        "Name": "old.example.com.",
        "Type": "A",
        "TTL": 300,
        "ResourceRecords": [{"Value": "10.0.0.9"}],
    }


@pytest.mark.asyncio
async def test_apply_record_delete_missing_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.com."}]
    }
    client.change_resource_record_sets.side_effect = _ClientError(
        "InvalidChangeBatch", "tried to delete a record that doesn't exist"
    )
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="delete",
        zone_name="example.com.",
        record=RecordData(name="ghost", record_type="A", value="10.0.0.9", ttl=300),
        target_serial=1,
    )
    # No raise — idempotent delete of an absent record.
    await driver._apply_record(_server(), CREDS, change)


@pytest.mark.asyncio
async def test_apply_record_wraps_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.com."}]
    }
    client.change_resource_record_sets.side_effect = _ClientError("AccessDenied", "not authorized")
    _patch_client(monkeypatch, driver, client)

    change = RecordChange(
        op="create",
        zone_name="example.com.",
        record=RecordData(name="www", record_type="A", value="10.0.0.1", ttl=300),
        target_serial=1,
    )
    with pytest.raises(CloudDNSError, match="change_resource_record_sets"):
        await driver._apply_record(_server(), CREDS, change)


# ── Zone write ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_zone_create(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    _patch_client(monkeypatch, driver, client)

    zone = SimpleNamespace(name="new.example.")
    await driver._apply_zone(_server(), CREDS, zone, "create")

    kwargs = client.create_hosted_zone.call_args.kwargs
    assert kwargs["Name"] == "new.example."
    # CallerReference must be a non-empty unique token.
    assert kwargs["CallerReference"]


@pytest.mark.asyncio
async def test_apply_zone_delete_resolves_id(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    client = MagicMock()
    client.list_hosted_zones_by_name.return_value = {
        "HostedZones": [{"Id": "/hostedzone/Z42", "Name": "doomed.example."}]
    }
    _patch_client(monkeypatch, driver, client)

    zone = SimpleNamespace(name="doomed.example.")
    await driver._apply_zone(_server(), CREDS, zone, "delete")

    assert client.delete_hosted_zone.call_args.kwargs["Id"] == "Z42"


@pytest.mark.asyncio
async def test_apply_zone_bad_op(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Route53DNSDriver()
    _patch_client(monkeypatch, driver, MagicMock())
    with pytest.raises(CloudDNSError, match="unsupported op"):
        await driver._apply_zone(_server(), CREDS, SimpleNamespace(name="x.test."), "rename")


# ── Capabilities ───────────────────────────────────────────────────────────


def test_capabilities_shape() -> None:
    caps = Route53DNSDriver().capabilities()
    assert caps["name"] == "route53"
    assert caps["agentless"] is True
    assert caps["manages_zones"] is True
    assert caps["dnssec_online"] is True
    assert caps["alias_records"] is True
    assert "ALIAS" in caps["record_types"]
    assert caps["views"] is False
    assert caps["rpz"] is False


def test_credential_fields() -> None:
    driver = Route53DNSDriver()
    assert driver.name == "route53"
    assert driver.credential_fields == ("access_key_id", "secret_access_key")
