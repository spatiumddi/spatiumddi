"""Web UI source restriction (#285 Phase 6).

`GET/PUT /appliance/firewall/web-ui-access` persists
``platform_settings.web_ui_allowed_cidrs`` (empty = open) with an anti-lockout
guard: a non-empty set that doesn't cover the operator's CURRENT source IP is
rejected 422 unless ``override_lockout=true``. The ASGI test transport reports
the caller as 127.0.0.1, so a set containing 127.0.0.0/8 is "covered" and one
that doesn't is the lockout case.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
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


async def test_get_default_open(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    await db_session.commit()
    j = (await client.get(f"{FW}/web-ui-access", headers=h)).json()
    assert j["allowed_cidrs"] == []
    assert j["open"] is True
    assert j["caller_ip"] == "127.0.0.1"
    assert j["caller_covered"] is True  # open covers everyone


async def test_put_covered_persists(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    await db_session.commit()
    r = await client.put(
        f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["127.0.0.0/8", "192.168.0.0/24"]}
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["allowed_cidrs"] == ["127.0.0.0/8", "192.168.0.0/24"]
    assert j["open"] is False and j["caller_covered"] is True
    cfg = await db_session.get(PlatformSettings, 1)
    assert cfg is not None and cfg.web_ui_allowed_cidrs == ["127.0.0.0/8", "192.168.0.0/24"]


async def test_put_lockout_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    # 127.0.0.1 (the test caller) is NOT in 10.0.0.0/8 → would lock out → 422.
    h = await _admin(db_session)
    await db_session.commit()
    r = await client.put(f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["10.0.0.0/8"]})
    assert r.status_code == 422
    assert "lock you out" in r.json()["detail"]
    # nothing persisted
    cfg = await db_session.get(PlatformSettings, 1)
    assert cfg is None or not cfg.web_ui_allowed_cidrs


async def test_put_lockout_override(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    await db_session.commit()
    r = await client.put(
        f"{FW}/web-ui-access",
        headers=h,
        json={"allowed_cidrs": ["10.0.0.0/8"], "override_lockout": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["caller_covered"] is False  # honestly reports the operator is now excluded
    cfg = await db_session.get(PlatformSettings, 1)
    assert cfg is not None and cfg.web_ui_allowed_cidrs == ["10.0.0.0/8"]


async def test_put_empty_reopens(client: AsyncClient, db_session: AsyncSession) -> None:
    # going back to open is never a lockout, so no guard fires.
    h = await _admin(db_session)
    db_session.add(PlatformSettings(id=1, web_ui_allowed_cidrs=["10.0.0.0/8"]))
    await db_session.commit()
    r = await client.put(f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": []})
    assert r.status_code == 200, r.text
    assert r.json()["open"] is True
    cfg = await db_session.get(PlatformSettings, 1)
    assert cfg is not None and cfg.web_ui_allowed_cidrs == []


async def test_put_canonicalises_and_dedupes(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    await db_session.commit()
    # host-bit-set + duplicate → canonicalised network + deduped.
    r = await client.put(
        f"{FW}/web-ui-access",
        headers=h,
        json={"allowed_cidrs": ["127.0.0.5/8", "127.0.0.0/8", "192.168.0.0/24"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["allowed_cidrs"] == ["127.0.0.0/8", "192.168.0.0/24"]


async def test_put_invalid_cidr_422(client: AsyncClient, db_session: AsyncSession) -> None:
    h = await _admin(db_session)
    await db_session.commit()
    r = await client.put(f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["not-a-cidr"]})
    assert r.status_code == 422


async def test_requires_admin(client: AsyncClient, db_session: AsyncSession) -> None:
    u = User(
        username=f"v-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="V",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db_session.add(u)
    await db_session.flush()
    await db_session.commit()
    h = {"Authorization": f"Bearer {create_access_token(str(u.id))}"}
    r = await client.put(f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": []})
    assert r.status_code == 403
