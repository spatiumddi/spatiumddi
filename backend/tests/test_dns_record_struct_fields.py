"""#424 — per-type structured fields on DNS records (priority/weight/port).

The three columns are the source of truth — every driver stitches them
into the wire format and silently substitutes 0 for a NULL, so a NULL
weight/port on an SRV renders as a meaningless ``prio 0 0 target`` (the UI
bug this issue fixes). The API now enforces the per-type rules:

* SRV requires priority + weight + port,
* MX takes only priority and defaults it to 10,
* every other type takes none of the three.
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


# ── SRV ──────────────────────────────────────────────────────────────────


async def test_srv_with_all_fields_succeeds(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={
            "name": "_sip._tcp",
            "record_type": "SRV",
            "value": "sipserver.example.com.",
            "priority": 10,
            "weight": 20,
            "port": 5060,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["priority"], body["weight"], body["port"]) == (10, 20, 5060)


async def test_srv_zero_values_are_kept_not_treated_as_missing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # 0 is a legitimate SRV priority/weight — the guard must check for
    # None, not falsiness.
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={
            "name": "_sip._udp",
            "record_type": "SRV",
            "value": "sip.example.com.",
            "priority": 0,
            "weight": 0,
            "port": 5060,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["weight"] == 0


async def test_srv_missing_weight_or_port_is_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    for payload in (
        {"priority": 10, "port": 5060},  # missing weight
        {"priority": 10, "weight": 20},  # missing port
        {"weight": 20, "port": 5060},  # missing priority
        {},  # missing all three
    ):
        r = await client.post(
            f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
            headers=h,
            json={
                "name": "_x._tcp",
                "record_type": "SRV",
                "value": "host.example.com.",
                **payload,
            },
        )
        assert r.status_code == 422, f"{payload}: {r.text}"
        assert "SRV records require" in r.text


# ── MX ───────────────────────────────────────────────────────────────────


async def test_mx_with_priority_succeeds(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={
            "name": "@",
            "record_type": "MX",
            "value": "mail.example.com.",
            "priority": 5,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["priority"] == 5


async def test_mx_without_priority_defaults_to_10(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "@", "record_type": "MX", "value": "mail2.example.com."},
    )
    assert r.status_code == 201, r.text
    # No more NULL priority on MX (the original bug); defaults to 10.
    assert r.json()["priority"] == 10


async def test_mx_with_weight_or_port_is_422(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={
            "name": "@",
            "record_type": "MX",
            "value": "mail.example.com.",
            "priority": 10,
            "weight": 5,
        },
    )
    assert r.status_code == 422, r.text
    assert "MX records take only a priority" in r.text


# ── Non-MX/SRV types reject stray structured fields ────────────────────────


async def test_a_record_rejects_priority(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={
            "name": "www",
            "record_type": "A",
            "value": "10.0.0.1",
            "priority": 10,
        },
    )
    assert r.status_code == 422, r.text
    assert "do not take" in r.text


async def test_a_record_with_no_struct_fields_succeeds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={"name": "www", "record_type": "A", "value": "10.0.0.1"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["priority"] is None and body["weight"] is None and body["port"] is None


# ── Update path ────────────────────────────────────────────────────────────


async def test_update_srv_port_succeeds(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={
            "name": "_sip._tcp",
            "record_type": "SRV",
            "value": "sip.example.com.",
            "priority": 10,
            "weight": 20,
            "port": 5060,
        },
    )
    rid = created.json()["id"]
    r = await client.put(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/{rid}",
        headers=h,
        json={"port": 5061},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The merged record keeps its other fields and applies the new port.
    assert (body["priority"], body["weight"], body["port"]) == (10, 20, 5061)


async def test_update_srv_unrelated_field_does_not_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Editing an unrelated field (TTL) on a fully-specified SRV must NOT
    # trip the per-type validation. This is the post-backfill guarantee
    # (migration f4a1c8e92b07 fills legacy NULL weight/port → 0 so every
    # existing SRV row stays editable).
    h = await _admin_headers(db_session)
    grp, zone = await _bind9_group(db_session)
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records",
        headers=h,
        json={
            "name": "_sip._tcp",
            "record_type": "SRV",
            "value": "sip.example.com.",
            "priority": 0,
            "weight": 0,
            "port": 5060,
        },
    )
    rid = created.json()["id"]
    r = await client.put(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/records/{rid}",
        headers=h,
        json={"ttl": 7200},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ttl"] == 7200
    assert (body["priority"], body["weight"], body["port"]) == (0, 0, 5060)
