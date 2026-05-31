"""Demo-mode lockdown for the Cloud integration (issue #335).

The Cloud integration is a read-only mirror whose ``test_connection``
probe accepts an inline, attacker-supplied credential body and makes an
outbound SDK call — an SSRF / outbound-probe channel on the public
Codespaces demo (which signs in as ``admin``/``admin`` superadmin).

Two mitigations are verified here:

1. ``integrations.cloud`` is in ``DEMO_RESTRICTED_MODULES`` so the whole
   ``/cloud`` router is force-disabled (404 via ``require_module``) in
   demo mode, exactly like the other five integration mirrors.
2. ``POST /cloud/endpoints/test`` carries its own inline
   ``forbid_in_demo_mode`` guard so the probe is blocked (403) even if
   the module gate were somehow open — defence in depth.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.demo_mode import DEMO_RESTRICTED_MODULES
from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.feature_module import FeatureModule
from app.services import feature_modules
from app.services.cloud.base import CloudProbeResult


def test_cloud_module_is_demo_restricted() -> None:
    """The cloud mirror must be locked down in demo mode like its
    sibling integration mirrors — otherwise the router (and its
    test-connection probe) stays live as an SSRF channel."""
    assert "integrations.cloud" in DEMO_RESTRICTED_MODULES


async def _make_admin(db: AsyncSession) -> str:
    user = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@example.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _enable_cloud_module(db: AsyncSession) -> None:
    """Force ``integrations.cloud`` on at the DB layer so the
    ``require_module`` router gate passes — lets the test isolate the
    *inline* demo guard on the test-connection handler."""
    db.add(FeatureModule(id="integrations.cloud", enabled=True))
    await db.flush()
    feature_modules.invalidate_cache()


@pytest.fixture(autouse=True)
def _reset_module_cache() -> Iterator[None]:
    feature_modules.invalidate_cache()
    yield
    feature_modules.invalidate_cache()


_TEST_BODY = {
    "provider": "aws",
    "credentials": {"access_key_id": "AKIA", "secret_access_key": "shh"},
    "provider_config": {},
    "regions": ["us-east-1"],
}


@patch("app.api.v1.cloud.router.get_connector")
async def test_test_connection_blocked_in_demo_mode(
    mock_get_connector: object,
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the module enabled (gate open) but demo mode on, the
    test-connection probe is refused with 403 and never reaches the
    connector — the outbound SDK call must not fire."""
    await _enable_cloud_module(db_session)
    token = await _make_admin(db_session)
    monkeypatch.setattr("app.config.settings.demo_mode", True)

    resp = await client.post(
        "/api/v1/cloud/endpoints/test",
        json=_TEST_BODY,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    assert "demo mode" in resp.json()["detail"].lower()
    mock_get_connector.assert_not_called()  # type: ignore[attr-defined]


@patch("app.api.v1.cloud.router.get_connector")
async def test_test_connection_reaches_connector_when_not_demo(
    mock_get_connector: object,
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: the 403 is demo-specific. With demo mode off the probe
    flows through to the connector (mocked), so the guard isn't a blanket
    block."""
    await _enable_cloud_module(db_session)
    token = await _make_admin(db_session)
    monkeypatch.setattr("app.config.settings.demo_mode", False)

    connector = AsyncMock()
    connector.probe = AsyncMock(
        return_value=CloudProbeResult(ok=True, message="ok", account_id="123456789012")
    )
    mock_get_connector.return_value = connector  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/v1/cloud/endpoints/test",
        json=_TEST_BODY,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_get_connector.assert_called_once()  # type: ignore[attr-defined]
