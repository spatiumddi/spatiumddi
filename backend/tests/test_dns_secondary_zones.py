"""Secondary / stub DNS zones — masters field + API validation (issue #336).

A ``type slave;`` / ``type stub;`` zone renders un-loadable BIND9 config
when no ``masters { ... };`` clause is present. These tests cover both the
renderer (masters render correctly) and the API guard (a secondary/stub
zone created or updated with no masters is rejected with 422).
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.drivers.dns.base import ZoneData
from app.drivers.dns.bind9 import BIND9Driver
from app.models.auth import User
from app.models.dns import DNSServerGroup, DNSZone


def _secondary_zone(masters: tuple[str, ...]) -> ZoneData:
    return ZoneData(
        name="example.com.",
        zone_type="secondary",
        kind="forward",
        ttl=3600,
        refresh=86400,
        retry=7200,
        expire=3600000,
        minimum=3600,
        primary_ns="",
        admin_email="",
        serial=0,
        masters=masters,
    )


# ── Renderer ────────────────────────────────────────────────────────────


def test_secondary_zone_renders_type_slave_with_masters() -> None:
    out = BIND9Driver().render_zone_config(_secondary_zone(("192.0.2.10",)))
    assert "type slave;" in out
    assert "masters { 192.0.2.10; };" in out
    # Secondaries pull the zone from the master — no inline-signing/DNSSEC.
    assert "dnssec-policy" not in out


def test_secondary_zone_master_with_port() -> None:
    out = BIND9Driver().render_zone_config(_secondary_zone(("192.0.2.10@5353", "192.0.2.11")))
    assert "192.0.2.10 port 5353;" in out
    assert "192.0.2.11;" in out


# ── API guard ─────────────────────────────────────────────────────────────


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _group(db: AsyncSession) -> DNSServerGroup:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    return grp


async def test_create_secondary_zone_without_masters_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp = await _group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones",
        headers=h,
        json={"name": "secondary.example.com.", "zone_type": "secondary"},
    )
    assert r.status_code == 422, r.text

    # Whitespace-only masters are treated as empty too.
    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones",
        headers=h,
        json={"name": "secondary.example.com.", "zone_type": "secondary", "masters": ["  "]},
    )
    assert r.status_code == 422, r.text


async def test_create_stub_zone_without_masters_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp = await _group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones",
        headers=h,
        json={"name": "stub.example.com.", "zone_type": "stub", "masters": []},
    )
    assert r.status_code == 422, r.text


async def test_create_secondary_zone_with_masters_succeeds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp = await _group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones",
        headers=h,
        json={
            "name": "secondary.example.com.",
            "zone_type": "secondary",
            "masters": ["192.0.2.10", "192.0.2.11@5353"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["zone_type"] == "secondary"
    assert body["masters"] == ["192.0.2.10", "192.0.2.11@5353"]


async def test_create_secondary_zone_rejects_injection_in_masters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # masters is rendered straight into ``masters { ... };`` — a value carrying
    # named-config metacharacters must be rejected, not persisted + injected
    # into the rendered server config (#336 config-injection hardening).
    h = await _admin_headers(db_session)
    grp = await _group(db_session)
    await db_session.commit()
    for bad in ('192.0.2.10; } ; zone "evil" { type master', "192.0.2.10 extra", "not-an-ip"):
        r = await client.post(
            f"/api/v1/dns/groups/{grp.id}/zones",
            headers=h,
            json={
                "name": "secondary.example.com.",
                "zone_type": "secondary",
                "masters": [bad],
            },
        )
        assert r.status_code == 422, f"{bad!r} should be rejected: {r.text}"


async def test_update_to_secondary_without_masters_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp = await _group(db_session)
    zone = DNSZone(
        group_id=grp.id,
        name="zone.example.com.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.example.com.",
        admin_email="admin.example.com.",
    )
    db_session.add(zone)
    await db_session.commit()

    # Flipping an existing primary to secondary without supplying masters
    # is the broken state we reject (issue #336).
    r = await client.put(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}",
        headers=h,
        json={"zone_type": "secondary"},
    )
    assert r.status_code == 422, r.text

    # Same flip WITH masters succeeds.
    r = await client.put(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}",
        headers=h,
        json={"zone_type": "secondary", "masters": ["192.0.2.20"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["masters"] == ["192.0.2.20"]


async def test_update_clearing_masters_on_secondary_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp = await _group(db_session)
    zone = DNSZone(
        group_id=grp.id,
        name="sec.example.com.",
        zone_type="secondary",
        kind="forward",
        masters=["192.0.2.30"],
    )
    db_session.add(zone)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}",
        headers=h,
        json={"masters": []},
    )
    assert r.status_code == 422, r.text
