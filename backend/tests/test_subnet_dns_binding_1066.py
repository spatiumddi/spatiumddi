"""spatiumddi#1066 (create) — ``POST /ipam/subnets`` keeps the DNS binding it
was given.

``SubnetCreate`` declared ``dns_zone_id`` twice and ``create_subnet`` built
the row with the field excluded, so a body naming ``dns_group_id`` +
``dns_zone_id`` answered 201 with ``dns_zone_id: null, dns_group_ids: []``,
an empty effective DNS, and a reverse zone auto-created from the very field
that was dropped — live on nightly-2026.09.13. Now the binding is stored, the
legacy singular ``dns_group_id`` seeds ``dns_group_ids``, a primary zone makes
the subnet's own DNS settings the effective ones, and a zone id that is not a
UUID is refused instead of dropped.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSServerGroup, DNSZone
from app.models.ipam import IPBlock, IPSpace


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="admin",
        hashed_password=hash_password("password123"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _fixture(db: AsyncSession) -> tuple[str, str, str, str]:
    """(space_id, block_id, group_id, forward zone id), committed."""
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:8]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.66.0.0/16", name="b")
    db.add(block)
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name="bind.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.bind.example.",
        admin_email="admin.bind.example.",
    )
    db.add(zone)
    await db.commit()
    return str(space.id), str(block.id), str(grp.id), str(zone.id)


async def _reverse_zone(db: AsyncSession, group_id: str, name: str) -> DNSZone | None:
    return (
        await db.execute(
            select(DNSZone)
            .where(DNSZone.group_id == uuid.UUID(group_id), DNSZone.name == name)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_post_with_group_and_zone_stores_the_binding_and_makes_it_effective(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    space_id, block_id, gid, zid = await _fixture(db_session)

    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": "10.66.1.0/24",
            "name": "bound",
            "dns_group_id": gid,
            "dns_zone_id": zid,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["dns_zone_id"] == zid
    assert body["dns_group_ids"] == [gid]
    assert body["dns_inherit_settings"] is False

    got = await client.get(f"/api/v1/ipam/subnets/{body['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["dns_zone_id"] == zid and got.json()["dns_group_ids"] == [gid]

    eff = await client.get(f"/api/v1/ipam/subnets/{body['id']}/effective-dns", headers=headers)
    assert eff.status_code == 200
    assert eff.json()["dns_zone_id"] == zid
    assert eff.json()["dns_group_ids"] == [gid]
    assert eff.json()["inherited_from_block_id"] is None

    rev = await _reverse_zone(db_session, gid, "1.66.10.in-addr.arpa.")
    assert rev is not None and rev.is_auto_generated is True
    assert str(rev.linked_subnet_id) == body["id"]


@pytest.mark.asyncio
async def test_post_with_the_plural_group_list_and_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    space_id, block_id, gid, zid = await _fixture(db_session)
    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": "10.66.2.0/24",
            "dns_group_ids": [gid],
            "dns_zone_id": zid,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["dns_zone_id"] == zid
    assert resp.json()["dns_group_ids"] == [gid]
    assert resp.json()["dns_inherit_settings"] is False


@pytest.mark.asyncio
async def test_post_with_the_group_alone_keeps_the_legacy_meaning(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``dns_group_id`` without a zone names where the reverse zone goes; it
    is kept on the row as the group binding but does not stop inheriting —
    a group without a primary zone publishes nothing either way."""
    headers = await _admin_headers(db_session)
    space_id, block_id, gid, _zid = await _fixture(db_session)
    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": "10.66.3.0/24",
            "dns_group_id": gid,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["dns_zone_id"] is None
    assert resp.json()["dns_group_ids"] == [gid]
    assert resp.json()["dns_inherit_settings"] is True
    rev = await _reverse_zone(db_session, gid, "3.66.10.in-addr.arpa.")
    assert rev is not None and str(rev.linked_subnet_id) == resp.json()["id"]


@pytest.mark.asyncio
async def test_an_explicit_inherit_true_does_not_survive_a_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The form with a template that prefills a zone sends ``dns_inherit_settings:
    true`` beside it; the zone wins, visibly, rather than being stored inert."""
    headers = await _admin_headers(db_session)
    space_id, block_id, gid, zid = await _fixture(db_session)
    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": "10.66.4.0/24",
            "dns_group_ids": [gid],
            "dns_zone_id": zid,
            "dns_inherit_settings": True,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["dns_inherit_settings"] is False


@pytest.mark.asyncio
async def test_a_zone_id_that_is_not_a_uuid_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    space_id, block_id, _gid, _zid = await _fixture(db_session)
    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": "10.66.5.0/24",
            "dns_zone_id": "not-a-zone",
        },
    )
    assert resp.status_code == 422, resp.text
    assert "dns_zone_id" in resp.text


@pytest.mark.asyncio
async def test_put_of_a_zone_makes_it_effective_too(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    space_id, block_id, gid, zid = await _fixture(db_session)
    created = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={"space_id": space_id, "block_id": block_id, "network": "10.66.6.0/24"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["dns_inherit_settings"] is True

    resp = await client.put(
        f"/api/v1/ipam/subnets/{created.json()['id']}",
        headers=headers,
        json={"dns_zone_id": zid, "dns_group_ids": [gid]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dns_zone_id"] == zid
    assert resp.json()["dns_inherit_settings"] is False
    eff = await client.get(
        f"/api/v1/ipam/subnets/{created.json()['id']}/effective-dns", headers=headers
    )
    assert eff.json()["dns_zone_id"] == zid
