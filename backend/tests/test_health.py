"""Health endpoint tests — success cases."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_structure(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    # May be 200 or 503 depending on test env; structure is always present
    body = response.json()
    assert "status" in body
    assert "checks" in body
