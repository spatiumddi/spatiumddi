"""#467 — A / AAAA record values must be a single valid IP address.

Each ``DNSRecord`` row holds exactly one address and the drivers render one
RR line per row, so a comma-separated value like ``10.0.0.1, 10.0.0.2`` was
silently stored verbatim and emitted as malformed rdata that breaks the zone
load. The create/update handlers now reject any A/AAAA value that isn't a
single IP of the matching family, pointing operators at one-record-per-IP
round-robin or a DNS Pool for health-checked failover.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSServer, DNSServerGroup, DNSZone


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


async def _bind9_group(db: AsyncSession) -> tuple[DNSServerGroup, DNSZone]:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    db.add(
        DNSServer(
            group_id=grp.id,
            driver="bind9",
            host="bind9.example.com",
            name=f"srv-{uuid.uuid4().hex[:6]}",
        )
    )
    zone = DNSZone(
        group_id=grp.id,
        name="example.com.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.example.com.",
        admin_email="admin.example.com.",
    )
    db.add(zone)
    await db.flush()
    return grp, zone


# ── Create path ────────────────────────────────────────────────────────────


async def test_single_ipv4_a_record_succeeds(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "10.0.0.1"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["value"] == "10.0.0.1"


async def test_single_ipv6_aaaa_record_succeeds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "AAAA", "value": "2001:db8::1"},
    )
    assert r.status_code == 201, r.text


async def test_surrounding_whitespace_is_accepted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # A pasted value with stray whitespace is a single IP — strip and accept.
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "  10.0.0.1  "},
    )
    assert r.status_code == 201, r.text


async def test_comma_separated_a_record_is_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The core footgun: two IPs in one record. Must be rejected with guidance.
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "10.0.0.1, 10.0.0.2"},
    )
    assert r.status_code == 422, r.text
    assert "single IPv4 address" in r.text
    assert "DNS Pool" in r.text


async def test_a_record_with_non_ip_value_is_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "not-an-ip"},
    )
    assert r.status_code == 422, r.text


async def test_a_record_rejects_ipv6_family_mismatch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "2001:db8::1"},
    )
    assert r.status_code == 422, r.text
    assert "single IPv4 address" in r.text


async def test_aaaa_record_rejects_ipv4_family_mismatch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "AAAA", "value": "10.0.0.1"},
    )
    assert r.status_code == 422, r.text
    assert "single IPv6 address" in r.text


async def test_non_address_types_are_unaffected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The guard only gates A/AAAA — a CNAME's hostname value (not an IP)
    # must still be accepted.
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "alias", "record_type": "CNAME", "value": "other.example.com."},
    )
    assert r.status_code == 201, r.text


# ── Update path ────────────────────────────────────────────────────────────


async def test_update_a_record_to_comma_list_is_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "10.0.0.1"},
    )
    rid = created.json()["id"]
    r = await client.put(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/{rid}",
        headers=h,
        json={"value": "10.0.0.1, 10.0.0.2"},
    )
    assert r.status_code == 422, r.text
    assert "single IPv4 address" in r.text


async def test_bulk_create_rejects_comma_separated_a_value(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The same guard applies on the API/Terraform bulk surface.
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/bulk-create",
        headers=h,
        json={
            "records": [
                {"name": "ok", "record_type": "A", "value": "10.0.0.1"},
                {"name": "bad", "record_type": "A", "value": "10.0.0.1, 10.0.0.2"},
            ]
        },
    )
    assert r.status_code == 422, r.text
    assert "single IPv4 address" in r.text


async def test_update_unrelated_field_on_a_record_does_not_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Editing TTL on a valid A record must not trip the address validation
    # (the merged value is still a single IP).
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "10.0.0.1"},
    )
    rid = created.json()["id"]
    r = await client.put(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/{rid}",
        headers=h,
        json={"ttl": 7200},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ttl"] == 7200
