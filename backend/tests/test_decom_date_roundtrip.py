"""Decom-date awareness API round-trips (#46).

* POST / PUT subnet round-trips ``decom_date`` and PUT can CLEAR it to
  null (exercises the explicit model_fields_set handler in
  ``update_subnet`` — the exclude_none dump would otherwise drop a null).
* POST / PUT ip_address round-trips ``decom_date`` (exclude_unset path
  clears null directly).
* ``SubnetResponse`` / ``IPAddressResponse`` surface the column.
* ``POST /alerts/rules`` accepts ``rule_type='decom_expiring'``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPBlock, IPSpace


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="admin",
        hashed_password=hash_password("password123"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_space_block(db: AsyncSession) -> tuple[str, str]:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:8]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="b")
    db.add(block)
    await db.commit()
    return str(space.id), str(block.id)


@pytest.mark.asyncio
async def test_subnet_decom_date_roundtrip_and_clear(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space_id, block_id = await _make_space_block(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # Create with a decom_date.
    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": "10.1.1.0/24",
            "name": "retiring",
            "decom_date": "2027-01-15",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["decom_date"] == "2027-01-15"
    subnet_id = body["id"]

    # Update the decom_date to a new value.
    resp = await client.put(
        f"/api/v1/ipam/subnets/{subnet_id}",
        headers=headers,
        json={"decom_date": "2028-06-01"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decom_date"] == "2028-06-01"

    # Clear it to null — exercises the explicit field-set handler.
    resp = await client.put(
        f"/api/v1/ipam/subnets/{subnet_id}",
        headers=headers,
        json={"decom_date": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decom_date"] is None

    # Confirm it persisted as null on a fresh GET.
    resp = await client.get(f"/api/v1/ipam/subnets/{subnet_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["decom_date"] is None


@pytest.mark.asyncio
async def test_ip_address_decom_date_roundtrip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space_id, block_id = await _make_space_block(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": "10.2.2.0/24",
            "name": "hosts",
        },
    )
    assert resp.status_code == 201, resp.text
    subnet_id = resp.json()["id"]

    # Create an address with a decom_date.
    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet_id}/addresses",
        headers=headers,
        json={
            "address": "10.2.2.50",
            "hostname": "host50",
            "status": "allocated",
            "decom_date": "2027-03-20",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["decom_date"] == "2027-03-20"
    ip_id = body["id"]

    # Update to a new decom_date.
    resp = await client.put(
        f"/api/v1/ipam/addresses/{ip_id}",
        headers=headers,
        json={"decom_date": "2027-09-09"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decom_date"] == "2027-09-09"

    # Clear it to null (exclude_unset path).
    resp = await client.put(
        f"/api/v1/ipam/addresses/{ip_id}",
        headers=headers,
        json={"decom_date": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decom_date"] is None


@pytest.mark.asyncio
async def test_alert_rule_decom_expiring_accepted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/alerts/rules",
        headers=headers,
        json={
            "name": "Decom soon",
            "rule_type": "decom_expiring",
            "threshold_days": 30,
            "severity": "warning",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["rule_type"] == "decom_expiring"
