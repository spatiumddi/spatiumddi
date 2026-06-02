"""Enforcement master switch + all-CP-hardened gate (#285 Phase 4a).

`GET/PUT /appliance/firewall/enforcement` flips platform_settings.firewall_enabled
but refuses to ENABLE until every reporting appliance node is hardened
(base_lanwide_k3s is False), unless override_unhardened is passed. Disabling is
never gated.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import User
from app.models.settings import PlatformSettings
from app.services.feature_modules import invalidate_cache

FW = "/api/v1/appliance/firewall"


@pytest.fixture(autouse=True)
def _reset_module_cache():
    invalidate_cache()
    yield
    invalidate_cache()


async def _admin(db: AsyncSession) -> dict:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(u.id))}"}


async def _appliance(db: AsyncSession, *, lanwide: bool | None, marker: str | None = "abc") -> None:
    der = os.urandom(32)
    db.add(
        Appliance(
            id=uuid.uuid4(),
            hostname=f"n-{uuid.uuid4().hex[:6]}",
            public_key_der=der,
            public_key_fingerprint=hashlib.sha256(der).hexdigest(),
            state=APPLIANCE_STATE_APPROVED,
            deployment_kind="appliance",
            base_conf_marker=marker,
            base_lanwide_k3s=lanwide,
        )
    )
    await db.flush()


async def test_status_empty_not_safe(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    await db_session.commit()
    j = (await client.get(f"{FW}/enforcement", headers=h)).json()
    assert j["enabled"] is False
    assert j["reported_count"] == 0
    assert j["safe_to_enable"] is False  # nothing reported → can't confirm


async def test_gate_blocks_enable_when_lanwide(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin(db_session)
    await _appliance(db_session, lanwide=False)  # hardened
    await _appliance(db_session, lanwide=True)  # still LAN-wide
    await db_session.commit()
    status = (await client.get(f"{FW}/enforcement", headers=h)).json()
    assert status["reported_count"] == 2 and status["hardened_count"] == 1
    assert status["all_hardened"] is False
    r = await client.put(f"{FW}/enforcement", headers=h, json={"enabled": True})
    assert r.status_code == 409
    assert "not yet hardened" in r.json()["detail"]


async def test_override_enables_despite_lanwide(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin(db_session)
    await _appliance(db_session, lanwide=True)
    await db_session.commit()
    r = await client.put(
        f"{FW}/enforcement", headers=h, json={"enabled": True, "override_unhardened": True}
    )
    assert r.status_code == 200 and r.json()["enabled"] is True


async def test_enable_allowed_when_all_hardened(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    h = await _admin(db_session)
    await _appliance(db_session, lanwide=False)
    await _appliance(db_session, lanwide=False)
    await db_session.commit()
    r = await client.put(f"{FW}/enforcement", headers=h, json={"enabled": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["enabled"] is True and j["all_hardened"] is True
    # the platform_settings row was actually flipped
    cfg = await db_session.get(PlatformSettings, 1)
    assert cfg is not None and cfg.firewall_enabled is True


async def test_disable_always_allowed(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    await _appliance(db_session, lanwide=True)  # unhardened, but disabling is fine
    db_session.add(PlatformSettings(id=1, firewall_enabled=True))
    await db_session.commit()
    r = await client.put(f"{FW}/enforcement", headers=h, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False


async def test_unknown_marker_node_blocks(client: AsyncClient, db_session: AsyncSession) -> None:
    # base_lanwide_k3s None (reported a marker but unknown classification) blocks.
    h = await _admin(db_session)
    await _appliance(db_session, lanwide=None, marker="x")
    await db_session.commit()
    status = (await client.get(f"{FW}/enforcement", headers=h)).json()
    assert status["reported_count"] == 1 and status["hardened_count"] == 0
    assert status["safe_to_enable"] is False
