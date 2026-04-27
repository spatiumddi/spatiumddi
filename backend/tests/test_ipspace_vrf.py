"""IPSpace VRF / routing-annotation column tests.

Pure annotation — address allocation does not consult these fields, so
we only need to verify the values round-trip through the API and that
explicit null clears them.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"vrf-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="VRF Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


@pytest.mark.asyncio
async def test_create_with_vrf_fields(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    resp = await client.post(
        "/api/v1/ipam/spaces",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "VRF-Test",
            "vrf_name": "RED",
            "route_distinguisher": "65000:100",
            "route_targets": ["import:65000:100", "export:65000:200"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["vrf_name"] == "RED"
    assert body["route_distinguisher"] == "65000:100"
    assert body["route_targets"] == [
        "import:65000:100",
        "export:65000:200",
    ]


@pytest.mark.asyncio
async def test_create_without_vrf_fields_defaults_null(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    resp = await client.post(
        "/api/v1/ipam/spaces",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Plain"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["vrf_name"] is None
    assert body["route_distinguisher"] is None
    assert body["route_targets"] is None


@pytest.mark.asyncio
async def test_update_persists_explicit_null(client: AsyncClient, db_session: AsyncSession) -> None:
    """Sending an explicit null clears a previously-set field."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/ipam/spaces",
        headers=headers,
        json={"name": "Mut", "vrf_name": "GREEN", "route_targets": ["a"]},
    )
    assert resp.status_code == 201
    space_id = resp.json()["id"]

    resp = await client.put(
        f"/api/v1/ipam/spaces/{space_id}",
        headers=headers,
        json={"vrf_name": None, "route_targets": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vrf_name"] is None
    assert body["route_targets"] is None


@pytest.mark.asyncio
async def test_empty_route_targets_list_is_distinct_from_null(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``[]`` is a legal value distinct from null — we round-trip it."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/ipam/spaces",
        headers=headers,
        json={"name": "EmptyRT", "route_targets": []},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["route_targets"] == []
