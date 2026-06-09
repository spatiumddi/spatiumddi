"""Integration tests for the system-wide maintenance-mode middleware
(issue #57).

Covers:

* GET / HEAD pass through even while maintenance mode is on.
* POST/PUT/PATCH/DELETE are 503'd with Retry-After + structured body when
  on (for a non-superadmin / unauthenticated caller) on a non-exempt path.
* A superadmin bearer bypasses the block.
* Exempt paths (auth + agent endpoints) pass through.
* PUT /settings stays reachable so a superadmin can disable it.
* Cache invalidation makes a toggle take effect on the next request.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import maintenance_mode
from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.settings import PlatformSettings


async def _make_user(
    db: AsyncSession,
    *,
    superadmin: bool,
    username: str,
) -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=superadmin,
    )
    user.groups = []  # mark loaded — is_effective_superadmin walks .groups
    db.add(user)
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


async def _set_maintenance(db: AsyncSession, *, enabled: bool, message: str = "") -> None:
    row = (
        await db.execute(select(PlatformSettings).where(PlatformSettings.id == 1))
    ).scalar_one_or_none()
    if row is None:
        row = PlatformSettings(id=1)
        db.add(row)
    row.maintenance_mode_enabled = enabled
    row.maintenance_message = message
    await db.commit()
    # The middleware reads its own short-TTL cache via its own session;
    # drop it so the next request reflects the just-committed state.
    maintenance_mode.invalidate_cache()


@pytest.fixture(autouse=True)
def _reset_maintenance_cache() -> None:
    # Belt-and-braces — each test starts from a cold cache regardless of
    # whatever a prior test left behind.
    maintenance_mode.invalidate_cache()


@pytest.mark.asyncio
async def test_read_passes_through_when_enabled(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _set_maintenance(db_session, enabled=True, message="db migration")
    # A read should never be blocked even while maintenance is on.
    resp = await client.get("/health/live")
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_mutation_blocked_when_enabled(
    db_session: AsyncSession, client: AsyncClient, method: str
) -> None:
    await _set_maintenance(db_session, enabled=True, message="back at 02:00 UTC")
    # An anonymous mutating request to a non-exempt path is 503'd before
    # auth even runs. The middleware blocks before routing, so the body is
    # irrelevant — use ``request(...)`` so httpx's ``delete`` (which takes
    # no ``json=`` kwarg) is exercised the same way as the others.
    resp = await client.request(method.upper(), "/api/v1/ipam/spaces", json={"name": "x"})
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("Retry-After") == "120"
    body = resp.json()
    assert body["maintenance"] is True
    assert body["message"] == "back at 02:00 UTC"
    assert "started_at" in body


@pytest.mark.asyncio
async def test_mutation_allowed_when_disabled(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _set_maintenance(db_session, enabled=False)
    # With maintenance off the middleware must NOT 503 — the request flows
    # to the handler (which 401s for lack of auth, proving we passed the
    # middleware).
    resp = await client.post("/api/v1/ipam/spaces", json={"name": "x"})
    assert resp.status_code != 503


@pytest.mark.asyncio
async def test_superadmin_bearer_bypasses(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="mmsuper")
    await db_session.commit()
    await _set_maintenance(db_session, enabled=True)
    headers = {"Authorization": f"Bearer {token}"}
    # The superadmin bypasses the 503; the request reaches the handler.
    resp = await client.post("/api/v1/ipam/spaces", headers=headers, json={"name": "mm-space"})
    assert resp.status_code != 503, resp.text


@pytest.mark.asyncio
async def test_non_superadmin_blocked(db_session: AsyncSession, client: AsyncClient) -> None:
    _, token = await _make_user(db_session, superadmin=False, username="mmplain")
    await db_session.commit()
    await _set_maintenance(db_session, enabled=True)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/v1/ipam/spaces", headers=headers, json={"name": "nope"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["maintenance"] is True


@pytest.mark.asyncio
async def test_settings_put_reachable_for_superadmin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    _, token = await _make_user(db_session, superadmin=True, username="mmtoggle")
    await db_session.commit()
    await _set_maintenance(db_session, enabled=True)
    headers = {"Authorization": f"Bearer {token}"}
    # /settings is exempt AND the caller is superadmin — disable via PUT.
    resp = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"maintenance_mode_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["maintenance_mode_enabled"] is False


@pytest.mark.asyncio
async def test_exempt_agent_path_passes(db_session: AsyncSession, client: AsyncClient) -> None:
    await _set_maintenance(db_session, enabled=True)
    # A POST to a DNS-agent endpoint must NOT be 503'd by the maintenance
    # middleware — agent config-caching is exempt (non-negotiable #5). It
    # may 401/422 at the handler, but it must clear the middleware.
    resp = await client.post("/api/v1/dns/agents/heartbeat", json={})
    assert resp.status_code != 503, resp.text


@pytest.mark.asyncio
async def test_exempt_auth_path_passes(db_session: AsyncSession, client: AsyncClient) -> None:
    await _set_maintenance(db_session, enabled=True)
    # Login must stay reachable so an admin can sign in to recover.
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "nobody", "password": "wrong"},
    )
    assert resp.status_code != 503


@pytest.mark.asyncio
async def test_cache_invalidation_takes_effect(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # Off initially → mutation passes the middleware.
    await _set_maintenance(db_session, enabled=False)
    resp = await client.post("/api/v1/ipam/spaces", json={"name": "x"})
    assert resp.status_code != 503
    # Flip on (which invalidates the cache) → next mutation is blocked.
    await _set_maintenance(db_session, enabled=True)
    resp = await client.post("/api/v1/ipam/spaces", json={"name": "x"})
    assert resp.status_code == 503
