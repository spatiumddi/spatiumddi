"""#571 — DNS GSLB pools must not be creatable on reverse zones.

Pools render A / AAAA records, which only make sense in a forward zone.
Reverse (in-addr.arpa / ip6.arpa) zones hold PTR records, so a pool there
could never render a valid record. The UI picker filters them out; this
covers the API-first defensive rejection so a direct API client can't
create an unrenderable pool either.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSServerGroup, DNSZone


async def _token(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _group_and_zone(
    db: AsyncSession, *, kind: str, name: str
) -> tuple[DNSServerGroup, DNSZone]:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name=name,
        zone_type="primary",
        kind=kind,
        primary_ns="ns1.example.com.",
        admin_email="admin.example.com.",
    )
    db.add(zone)
    await db.flush()
    return grp, zone


def _pool_body() -> dict:
    return {"name": "web", "record_name": "www", "record_type": "A"}


@pytest.mark.asyncio
async def test_pool_create_rejected_on_reverse_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _token(db_session)
    grp, zone = await _group_and_zone(db_session, kind="reverse", name="10.in-addr.arpa.")
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/pools",
        headers=h,
        json=_pool_body(),
    )
    assert r.status_code == 400, r.text
    assert "reverse" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_pool_create_allowed_on_forward_zone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _token(db_session)
    grp, zone = await _group_and_zone(db_session, kind="forward", name="example.com.")
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        f"/api/v1/dns/groups/{grp.id}/zones/{zone.id}/pools",
        headers=h,
        json=_pool_body(),
    )
    assert r.status_code == 201, r.text
