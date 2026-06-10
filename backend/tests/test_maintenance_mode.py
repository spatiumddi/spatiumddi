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

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import maintenance_mode
from app.core.security import (
    create_access_token,
    generate_api_token,
    hash_password,
)
from app.models.auth import APIToken, User, UserSession
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


@pytest.mark.asyncio
async def test_revoked_session_superadmin_jwt_does_not_bypass(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """FIX 1 — a force-logged-out superadmin (UserSession.revoked) whose JWT is
    still unexpired must NOT bypass maintenance mode (mirrors the auth dep's
    session gate). The jti-bearing token is still 503'd."""
    user, _ = await _make_user(db_session, superadmin=True, username="mmrevoked")
    # Mint a session row, then revoke it, but keep the (still-unexpired) JWT.
    jti = uuid.uuid4().hex
    db_session.add(
        UserSession(
            id=jti,
            user_id=user.id,
            refresh_token_hash=uuid.uuid4().hex,
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            revoked=True,
        )
    )
    await db_session.commit()
    token = create_access_token(str(user.id), jti=jti)
    await _set_maintenance(db_session, enabled=True)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/v1/ipam/spaces", headers=headers, json={"name": "nope"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["maintenance"] is True


@pytest.mark.asyncio
async def test_live_session_superadmin_jwt_bypasses(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Counterpart to FIX 1 — a superadmin with a LIVE (non-revoked, unexpired)
    session still bypasses, so the session gate doesn't over-block."""
    user, _ = await _make_user(db_session, superadmin=True, username="mmlive")
    jti = uuid.uuid4().hex
    db_session.add(
        UserSession(
            id=jti,
            user_id=user.id,
            refresh_token_hash=uuid.uuid4().hex,
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            revoked=False,
        )
    )
    await db_session.commit()
    token = create_access_token(str(user.id), jti=jti)
    await _set_maintenance(db_session, enabled=True)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/v1/ipam/spaces", headers=headers, json={"name": "mm-live"})
    assert resp.status_code != 503, resp.text


async def _make_api_token(db: AsyncSession, *, user: User, scopes: list[str]) -> str:
    raw, prefix, token_hash = generate_api_token()
    db.add(
        APIToken(
            name="mm-tok",
            token_hash=token_hash,
            prefix=prefix,
            scope="user",
            user_id=user.id,
            created_by_user_id=user.id,
            scopes=scopes,
            is_active=True,
        )
    )
    await db.flush()
    return raw


@pytest.mark.asyncio
async def test_readonly_scoped_superadmin_token_does_not_bypass_write(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """FIX 2 — a superadmin's read-only-scoped ``sddi_`` token must NOT bypass a
    write during maintenance (mirrors deps._resolve_api_token's scope gate: a
    read-only token can never reach a write handler)."""
    user, _ = await _make_user(db_session, superadmin=True, username="mmtokread")
    raw = await _make_api_token(db_session, user=user, scopes=["read"])
    await db_session.commit()
    await _set_maintenance(db_session, enabled=True)
    headers = {"Authorization": f"Bearer {raw}"}
    # A write (POST) on a non-exempt path with a read-only token → still 503.
    resp = await client.post("/api/v1/ipam/spaces", headers=headers, json={"name": "nope"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["maintenance"] is True


@pytest.mark.asyncio
async def test_write_scoped_superadmin_token_bypasses(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Counterpart to FIX 2 — a superadmin token scoped ``ipam:write`` DOES
    bypass a write to an IPAM path, so the scope gate doesn't over-block."""
    user, _ = await _make_user(db_session, superadmin=True, username="mmtokwrite")
    raw = await _make_api_token(db_session, user=user, scopes=["ipam:write"])
    await db_session.commit()
    await _set_maintenance(db_session, enabled=True)
    headers = {"Authorization": f"Bearer {raw}"}
    resp = await client.post("/api/v1/ipam/spaces", headers=headers, json={"name": "mm-tok-write"})
    assert resp.status_code != 503, resp.text


@pytest.mark.asyncio
async def test_self_register_bootstrap_path_exempt(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The local-supervisor self-bootstrap POST must clear the maintenance
    middleware (non-negotiable #5) — it may 403/422 at the handler but must
    not be 503'd."""
    await _set_maintenance(db_session, enabled=True)
    resp = await client.post(
        "/api/v1/appliance/self-register-bootstrap",
        json={"appliance_variant": "full-stack"},
    )
    assert resp.status_code != 503, resp.text
