"""HTTP-level tests for the built-in network tools router (#58).

All subprocess + socket work is mocked — no packets leave the box, no
binary is required. Redis (the rate-limit backend) is mocked too. We
verify the router contract:

* 200 with valid (mocked) input;
* 403 without the ``use_network_tools`` permission;
* 404 when the ``tools.network`` feature module is disabled
  (``require_module``);
* 429 when the per-user budget is exceeded;
* 422 on a bad target / port / record_type.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tools.schemas import CommandResult, PortTestResult, TlsCertResult
from app.core.security import create_access_token, hash_password
from app.models.auth import Group, Role, User
from app.models.feature_module import FeatureModule
from app.services import feature_modules


async def _make_superadmin(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


async def _make_user_with_perm(db: AsyncSession, perm: dict | None) -> tuple[User, str]:
    """Create a non-superadmin user; optionally grant ``perm`` via a
    role + group so the permission helper sees it."""
    u = User(
        username=f"user-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="User",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db.add(u)
    await db.flush()
    if perm is not None:
        role = Role(name=f"r-{uuid.uuid4().hex[:6]}", description="", permissions=[perm])
        db.add(role)
        await db.flush()
        group = Group(name=f"g-{uuid.uuid4().hex[:6]}", description="")
        group.roles = [role]
        group.users = [u]
        db.add(group)
        await db.flush()
    return u, create_access_token(str(u.id))


@pytest.fixture(autouse=True)
def _reset_module_cache() -> None:
    """The feature-module enabled-set is process-cached. Reset it around
    every test so a 404-module-disabled test can't leak its override into
    a later test (or vice versa)."""
    feature_modules.invalidate_cache()
    yield
    feature_modules.invalidate_cache()


def _no_limit_redis() -> AsyncMock:
    r = AsyncMock()
    r.incr = AsyncMock(return_value=1)
    r.expire = AsyncMock()
    r.aclose = AsyncMock()
    return r


def _over_limit_redis() -> AsyncMock:
    r = AsyncMock()
    r.incr = AsyncMock(return_value=999)  # way over any budget
    r.expire = AsyncMock()
    r.aclose = AsyncMock()
    return r


async def test_ping_200_with_perm(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    fake = CommandResult(tool="ping", argv=["ping"], available=True, exit_code=0, stdout="ok")
    with (
        patch("app.api.v1.tools.router.run_ping", AsyncMock(return_value=fake)),
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True


async def test_port_test_200(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    fake = PortTestResult(host="10.0.0.1", port=443, protocol="tcp", state="open", rtt_ms=1.2)
    with (
        patch("app.api.v1.tools.router.test_port", AsyncMock(return_value=fake)),
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/port-test",
            json={"host": "10.0.0.1", "port": 443},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "open"


async def test_tls_cert_200(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    fake = TlsCertResult(host="x", port=443, server_name="x", ok=True, days_remaining=42)
    with (
        patch("app.api.v1.tools.router.inspect_tls_cert", AsyncMock(return_value=fake)),
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/tls-cert",
            json={"host": "example.com"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


async def test_mac_vendor_surfaces_oui_disabled(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    with patch(
        "app.services.nettools.throttle.make_async_redis",
        return_value=_no_limit_redis(),
    ):
        r = await client.post(
            "/api/v1/tools/mac-vendor",
            json={"macs": ["00:11:22:33:44:55"]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # No PlatformSettings row in the test DB → oui disabled.
    assert body["oui_enabled"] is False
    assert body["entries"][0]["mac"] == "00:11:22:33:44:55"


async def test_grant_permission_allows_access(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user_with_perm(
        db_session, {"action": "admin", "resource_type": "use_network_tools"}
    )
    fake = CommandResult(tool="ping", argv=["ping"], available=True, exit_code=0, stdout="ok")
    with (
        patch("app.api.v1.tools.router.run_ping", AsyncMock(return_value=fake)),
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text


async def test_403_without_permission(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user_with_perm(db_session, None)  # no perms at all
    with patch(
        "app.services.nettools.throttle.make_async_redis",
        return_value=_no_limit_redis(),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 403, r.text


async def test_404_when_module_disabled(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    # Disable the feature module via an override row.
    db_session.add(FeatureModule(id="tools.network", enabled=False))
    await db_session.flush()
    feature_modules.invalidate_cache()
    try:
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404, r.text
    finally:
        feature_modules.invalidate_cache()


async def test_429_when_rate_limited(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    fake = CommandResult(tool="ping", argv=["ping"], available=True, exit_code=0, stdout="ok")
    with (
        patch("app.api.v1.tools.router.run_ping", AsyncMock(return_value=fake)),
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_over_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/tools/ping", {"host": "1.1.1.1; rm -rf /"}),
        ("/api/v1/tools/ping", {"host": "bad host with spaces"}),
        ("/api/v1/tools/port-test", {"host": "10.0.0.1", "port": 70000}),
        ("/api/v1/tools/port-test", {"host": "10.0.0.1", "port": 22, "protocol": "icmp"}),
        ("/api/v1/tools/dig", {"name": "example.com", "record_type": "EVIL"}),
    ],
)
async def test_422_on_bad_input(
    client: AsyncClient, db_session: AsyncSession, path: str, payload: dict
) -> None:
    _, token = await _make_superadmin(db_session)
    with patch(
        "app.services.nettools.throttle.make_async_redis",
        return_value=_no_limit_redis(),
    ):
        r = await client.post(path, json=payload, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422, r.text
