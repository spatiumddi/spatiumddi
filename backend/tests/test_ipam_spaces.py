"""IPAM IP Space endpoint tests — success, unauthorized, validation."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User


async def _make_user(db: AsyncSession, superadmin: bool = False) -> tuple[User, str]:
    user = User(
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        hashed_password=hash_password("password123"),
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


@pytest.mark.asyncio
async def test_list_spaces_unauthorized(client: AsyncClient) -> None:
    response = await client.get("/api/v1/ipam/spaces")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_and_list_space(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/ipam/spaces",
        json={"name": "Corporate", "description": "Main corporate space"},
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Corporate"
    space_id = body["id"]

    list_response = await client.get("/api/v1/ipam/spaces", headers=headers)
    assert list_response.status_code == 200
    ids = [s["id"] for s in list_response.json()]
    assert space_id in ids


@pytest.mark.asyncio
async def test_create_space_invalid_payload(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # Missing required 'name' field
    response = await client.post("/api/v1/ipam/spaces", json={}, headers=headers)
    assert response.status_code == 422
