"""Tests for the cloud DNS live-pull importer (issue #37, Part B).

The four cloud drivers (Cloudflare / Route 53 / Azure DNS / Google
Cloud DNS) hit a real provider API, so we monkeypatch the ``get_driver``
registry lookup the importer uses and return a fake driver whose
``pull_zones_from_server`` / ``pull_zone_records`` return synthetic data.
This exercises the same canonical-shape coercion + conflict-detection
paths the production importer hits, entirely offline.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dns.base import RecordData
from app.models.dns import DNSServer, DNSServerGroup, DNSZone
from app.services.dns_import import cloud as cloud_import

# ── Helpers ──────────────────────────────────────────────────────────


async def _make_group(db: AsyncSession, name: str = "cloud-grp") -> DNSServerGroup:
    g = DNSServerGroup(name=name)
    db.add(g)
    await db.flush()
    return g


async def _make_cloud_server(
    db: AsyncSession,
    group: DNSServerGroup,
    *,
    name: str = "cf01",
    driver: str = "cloudflare",
    host: str = "api.cloudflare.com",
) -> DNSServer:
    server = DNSServer(
        group_id=group.id,
        name=name,
        driver=driver,
        host=host,
        port=443,
        is_enabled=True,
        # The fake driver never decrypts this; the importer only reads
        # zone / record methods. Any placeholder blob works.
        credentials_encrypted=b"placeholder-encrypted-blob",
    )
    db.add(server)
    await db.flush()
    return server


# Synthetic provider data. Mirrors CloudDNSDriverBase.pull_zones_from_server
# (trailing-dot FQDN, "Primary" zone_type, is_reverse_lookup +
# dnssec_enabled flags) + pull_zone_records (RecordData relative to apex).
_FAKE_ZONES = [
    {
        "name": "example.com.",
        "zone_type": "Primary",
        "is_reverse_lookup": False,
        "dnssec_enabled": True,
        "zone_id": "cf-zone-1",
        "record_count": 3,
    },
    {
        "name": "10.in-addr.arpa.",
        "zone_type": "Primary",
        "is_reverse_lookup": True,
        "dnssec_enabled": False,
        "zone_id": "cf-zone-2",
        "record_count": 1,
    },
]

_FAKE_RECORDS = {
    "example.com.": [
        RecordData(name="@", record_type="A", value="203.0.113.10", ttl=300),
        RecordData(name="www", record_type="A", value="203.0.113.10", ttl=300),
        RecordData(
            name="@",
            record_type="MX",
            value="mail.example.com.",
            ttl=3600,
            priority=10,
        ),
    ],
    "10.in-addr.arpa.": [
        RecordData(name="10.0.0", record_type="PTR", value="host.example.com.", ttl=3600),
    ],
}


class _FakeCloudDriver:
    """Stub matching the CloudDNSDriverBase read surface the importer uses."""

    async def pull_zones_from_server(self, server):  # noqa: ARG002 — signature parity
        return [dict(z) for z in _FAKE_ZONES]

    async def pull_zone_records(self, server, zone_name):  # noqa: ARG002
        # Normalise to the trailing-dot key the importer passes in.
        key = zone_name if zone_name.endswith(".") else zone_name + "."
        return list(_FAKE_RECORDS.get(key, []))


@pytest.fixture
def patched_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the registry lookup so the importer gets the fake driver."""

    monkeypatch.setattr(cloud_import, "get_driver", lambda _name: _FakeCloudDriver())


# ── Service-layer ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_cloud_import_canonical_shape(
    db_session: AsyncSession, patched_driver: None
) -> None:
    group = await _make_group(db_session)
    server = await _make_cloud_server(db_session, group)
    await db_session.commit()

    preview = await cloud_import.preview_cloud_import(
        db_session, server_id=server.id, target_group_id=group.id
    )

    # Source label carries the provider name, not a generic "cloud".
    assert preview.source == "cloudflare"
    assert len(preview.zones) == 2
    assert preview.conflicts == []  # nothing in the target group yet

    by_name = {z.name: z for z in preview.zones}
    assert "example.com." in by_name
    assert "10.in-addr.arpa." in by_name

    forward = by_name["example.com."]
    assert forward.kind == "forward"
    assert forward.zone_type == "primary"
    assert {r.record_type for r in forward.records} == {"A", "MX"}
    mx = next(r for r in forward.records if r.record_type == "MX")
    assert mx.priority == 10
    # SOA defaults applied with the operator-facing warning.
    assert forward.soa is not None
    assert forward.soa.ttl == 3600
    assert forward.soa.refresh == 86400
    assert any("SOA defaults" in w for w in forward.parse_warnings)
    # DNSSEC-signed source surfaces a per-zone warning.
    assert any("DNSSEC-signed" in w for w in forward.parse_warnings)

    rev = by_name["10.in-addr.arpa."]
    assert rev.kind == "reverse"
    # Reverse zone is not signed → no DNSSEC warning.
    assert not any("DNSSEC-signed" in w for w in rev.parse_warnings)

    # Histogram aggregates across zones.
    assert preview.record_type_histogram["A"] == 2
    assert preview.record_type_histogram["MX"] == 1
    assert preview.record_type_histogram["PTR"] == 1
    assert preview.total_records == 4


@pytest.mark.asyncio
async def test_preview_cloud_import_flags_conflict(
    db_session: AsyncSession, patched_driver: None
) -> None:
    group = await _make_group(db_session)
    server = await _make_cloud_server(db_session, group)
    # Pre-create a zone that collides with one of the fake zones.
    existing = DNSZone(
        group_id=group.id,
        name="example.com.",
        zone_type="primary",
        kind="forward",
    )
    db_session.add(existing)
    await db_session.commit()

    preview = await cloud_import.preview_cloud_import(
        db_session, server_id=server.id, target_group_id=group.id
    )

    assert {c.zone_name for c in preview.conflicts} == {"example.com."}
    conflict = preview.conflicts[0]
    assert conflict.existing_zone_id == str(existing.id)
    # The non-colliding reverse zone is not flagged.
    assert "10.in-addr.arpa." not in {c.zone_name for c in preview.conflicts}


@pytest.mark.asyncio
async def test_preview_rejects_non_cloud_driver(
    db_session: AsyncSession, patched_driver: None
) -> None:
    group = await _make_group(db_session)
    bind_server = await _make_cloud_server(
        db_session, group, name="bind1", driver="bind9", host="bind1.example.com"
    )
    await db_session.commit()

    with pytest.raises(ValueError) as exc_info:
        await cloud_import.preview_cloud_import(
            db_session, server_id=bind_server.id, target_group_id=group.id
        )
    assert "bind9" in str(exc_info.value)


@pytest.mark.asyncio
async def test_preview_rejects_unknown_server(db_session: AsyncSession) -> None:
    group = await _make_group(db_session)
    await db_session.commit()

    with pytest.raises(ValueError) as exc_info:
        await cloud_import.preview_cloud_import(
            db_session, server_id=uuid.uuid4(), target_group_id=group.id
        )
    assert "does not exist" in str(exc_info.value)


@pytest.mark.asyncio
async def test_preview_empty_account_warns(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = await _make_group(db_session)
    server = await _make_cloud_server(db_session, group)
    await db_session.commit()

    class _EmptyDriver:
        async def pull_zones_from_server(self, server):  # noqa: ARG002
            return []

        async def pull_zone_records(self, server, zone_name):  # noqa: ARG002
            return []

    monkeypatch.setattr(cloud_import, "get_driver", lambda _name: _EmptyDriver())

    preview = await cloud_import.preview_cloud_import(
        db_session, server_id=server.id, target_group_id=group.id
    )
    assert preview.zones == []
    assert preview.total_records == 0
    assert any("No hosted zones" in w for w in preview.warnings)
