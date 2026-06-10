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
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tools.schemas import CommandResult, PortTestResult, TlsCertResult
from app.core.security import create_access_token, hash_password
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import Group, Role, User
from app.models.feature_module import FeatureModule
from app.services import feature_modules
from app.services.appliance import agent_cmd


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


async def _make_appliance(
    db: AsyncSession,
    *,
    state: str = APPLIANCE_STATE_APPROVED,
    last_seen_offset_s: float | None = 5.0,
) -> Appliance:
    """Create a Fleet appliance row. ``last_seen_offset_s`` is how long
    ago the last heartbeat was (None ⇒ never heartbeated)."""
    last_seen = (
        None
        if last_seen_offset_s is None
        else datetime.now(UTC) - timedelta(seconds=last_seen_offset_s)
    )
    appliance = Appliance(
        hostname=f"appl-{uuid.uuid4().hex[:6]}",
        public_key_der=b"\x00" * 44,
        # 64-char unique hex fingerprint.
        public_key_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        state=state,
        last_seen_at=last_seen,
    )
    db.add(appliance)
    await db.flush()
    return appliance


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


# ── agent-perspective dispatch (dashboard-and-remote-nettools) ──────


async def test_appliance_target_enqueues_and_labels_ran_from(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An appliance target enqueues + returns the mocked supervisor
    result with ran_from stamped to the appliance hostname."""
    _, token = await _make_superadmin(db_session)
    appliance = await _make_appliance(db_session)

    supervisor_result = agent_cmd.NetToolResult(
        request_id="x",
        result={"tool": "ping", "argv": ["ping"], "available": True, "exit_code": 0},
    )
    with (
        patch(
            "app.api.v1.tools.router.agent_cmd.enqueue_command",
            AsyncMock(return_value=supervisor_result),
        ) as mock_enqueue,
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1", "target": {"kind": "appliance", "id": str(appliance.id)}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["ran_from"] == f"appliance:{appliance.hostname}"
    # The queue was hit with the validated tool + params (no nested target).
    mock_enqueue.assert_awaited_once()
    call = mock_enqueue.await_args
    assert call.args[1] == "ping"  # tool
    assert "target" not in call.args[2]  # params shipped to the wire
    assert call.args[2]["host"] == "1.1.1.1"


async def test_appliance_target_on_server_only_tool_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """mtr shares HostRequest but isn't in the reachability set — an
    appliance target must 400 (not silently run on the server)."""
    _, token = await _make_superadmin(db_session)
    appliance = await _make_appliance(db_session)
    with patch(
        "app.services.nettools.throttle.make_async_redis",
        return_value=_no_limit_redis(),
    ):
        r = await client.post(
            "/api/v1/tools/mtr",
            json={"host": "1.1.1.1", "target": {"kind": "appliance", "id": str(appliance.id)}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 400, r.text


async def test_appliance_target_unknown_appliance_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_superadmin(db_session)
    with patch(
        "app.services.nettools.throttle.make_async_redis",
        return_value=_no_limit_redis(),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1", "target": {"kind": "appliance", "id": str(uuid.uuid4())}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 404, r.text


async def test_appliance_target_ssrf_blocked_host_still_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The SSRF denylist applies server-side even for an appliance
    target — a port-test aimed at the cloud-metadata IP is rejected at
    schema validation (422) before anything is enqueued."""
    _, token = await _make_superadmin(db_session)
    appliance = await _make_appliance(db_session)
    with (
        patch(
            "app.api.v1.tools.router.agent_cmd.enqueue_command",
            AsyncMock(),
        ) as mock_enqueue,
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/port-test",
            json={
                "host": "169.254.169.254",
                "port": 80,
                "target": {"kind": "appliance", "id": str(appliance.id)},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 422, r.text
    mock_enqueue.assert_not_awaited()


async def test_appliance_target_offline_503(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    appliance = await _make_appliance(db_session)
    with (
        patch(
            "app.api.v1.tools.router.agent_cmd.enqueue_command",
            AsyncMock(side_effect=agent_cmd.ApplianceOffline("offline")),
        ),
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1", "target": {"kind": "appliance", "id": str(appliance.id)}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 503, r.text


async def test_appliance_target_timeout_504(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_superadmin(db_session)
    appliance = await _make_appliance(db_session)
    with (
        patch(
            "app.api.v1.tools.router.agent_cmd.enqueue_command",
            AsyncMock(side_effect=TimeoutError()),
        ),
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/tls-cert",
            json={"host": "example.com", "target": {"kind": "appliance", "id": str(appliance.id)}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 504, r.text


async def test_server_target_explicit_still_runs_inline(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An explicit kind='server' target is identical to omitting it —
    runs inline, ran_from='server', never touches the queue."""
    _, token = await _make_superadmin(db_session)
    fake = CommandResult(tool="ping", argv=["ping"], available=True, exit_code=0, stdout="ok")
    with (
        patch("app.api.v1.tools.router.run_ping", AsyncMock(return_value=fake)),
        patch("app.api.v1.tools.router.agent_cmd.enqueue_command", AsyncMock()) as mock_enqueue,
        patch(
            "app.services.nettools.throttle.make_async_redis",
            return_value=_no_limit_redis(),
        ),
    ):
        r = await client.post(
            "/api/v1/tools/ping",
            json={"host": "1.1.1.1", "target": {"kind": "server"}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["ran_from"] == "server"
    mock_enqueue.assert_not_awaited()
