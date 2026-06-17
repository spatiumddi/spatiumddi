"""Saved searches / saved views (issue #77) — CRUD + scoping + MCP tools.

Covers the per-user ownership invariant (one user can't see/modify
another's views), the per-(user, page, name) uniqueness 409, the
at-most-one-default-per-page rule, and the two read-only MCP tools.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.saved_view import SavedView
from app.services import feature_modules
from app.services.ai.tools.saved_views import (
    CountSavedViewsArgs,
    FindSavedViewsArgs,
    count_saved_views,
    find_saved_views,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _reset_module_cache():
    feature_modules.invalidate_cache()
    yield
    feature_modules.invalidate_cache()


async def _user(db: AsyncSession) -> tuple[User, str]:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="U",
        hashed_password=hash_password("x"),
        is_superadmin=False,
    )
    db.add(u)
    await db.flush()
    return u, create_access_token(str(u.id))


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_create_list_update_delete_roundtrip(client: AsyncClient, db_session):
    _, token = await _user(db_session)
    h = _hdr(token)

    r = await client.post(
        "/api/v1/saved-views",
        headers=h,
        json={
            "page": "network.services",
            "name": "Active in DC1",
            "payload": {"status": "active", "search": "dc1"},
        },
    )
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["name"] == "Active in DC1"
    assert view["payload"]["status"] == "active"
    assert view["is_default"] is False
    vid = view["id"]

    # List (page filter hits, non-matching page misses).
    r = await client.get("/api/v1/saved-views", headers=h, params={"page": "network.services"})
    assert r.status_code == 200
    assert [v["id"] for v in r.json()] == [vid]
    r = await client.get("/api/v1/saved-views", headers=h, params={"page": "network.circuits"})
    assert r.json() == []

    # Update payload + rename.
    r = await client.patch(
        f"/api/v1/saved-views/{vid}",
        headers=h,
        json={"name": "Active everywhere", "payload": {"status": "active"}},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Active everywhere"
    assert "search" not in r.json()["payload"]

    # Delete.
    r = await client.delete(f"/api/v1/saved-views/{vid}", headers=h)
    assert r.status_code == 204
    r = await client.get("/api/v1/saved-views", headers=h)
    assert r.json() == []


async def test_duplicate_name_same_page_conflicts(client: AsyncClient, db_session):
    _, token = await _user(db_session)
    h = _hdr(token)
    body = {"page": "network.services", "name": "dupe", "payload": {}}
    assert (await client.post("/api/v1/saved-views", headers=h, json=body)).status_code == 201
    r = await client.post("/api/v1/saved-views", headers=h, json=body)
    assert r.status_code == 409
    # Same name on a *different* page is fine.
    body2 = {"page": "network.circuits", "name": "dupe", "payload": {}}
    assert (await client.post("/api/v1/saved-views", headers=h, json=body2)).status_code == 201


async def test_at_most_one_default_per_page(client: AsyncClient, db_session):
    _, token = await _user(db_session)
    h = _hdr(token)
    a = (
        await client.post(
            "/api/v1/saved-views",
            headers=h,
            json={"page": "p", "name": "a", "payload": {}, "is_default": True},
        )
    ).json()
    b = (
        await client.post(
            "/api/v1/saved-views",
            headers=h,
            json={"page": "p", "name": "b", "payload": {}, "is_default": True},
        )
    ).json()
    # Creating b as default must have cleared a's default flag.
    rows = {v["id"]: v for v in (await client.get("/api/v1/saved-views", headers=h)).json()}
    assert rows[a["id"]]["is_default"] is False
    assert rows[b["id"]]["is_default"] is True


async def test_other_user_cannot_see_or_touch(client: AsyncClient, db_session):
    _, t1 = await _user(db_session)
    _, t2 = await _user(db_session)
    vid = (
        await client.post(
            "/api/v1/saved-views",
            headers=_hdr(t1),
            json={"page": "p", "name": "mine", "payload": {}},
        )
    ).json()["id"]

    # User 2 sees an empty list and gets 404 on user 1's view.
    assert (await client.get("/api/v1/saved-views", headers=_hdr(t2))).json() == []
    assert (
        await client.patch(f"/api/v1/saved-views/{vid}", headers=_hdr(t2), json={"name": "hijack"})
    ).status_code == 404
    assert (await client.delete(f"/api/v1/saved-views/{vid}", headers=_hdr(t2))).status_code == 404


async def test_mcp_tools_scope_to_caller(client: AsyncClient, db_session):
    user, token = await _user(db_session)
    other, _ = await _user(db_session)
    db_session.add(SavedView(user_id=user.id, page="p", name="x", payload={}))
    db_session.add(SavedView(user_id=user.id, page="q", name="y", payload={}))
    db_session.add(SavedView(user_id=other.id, page="p", name="z", payload={}))
    await db_session.flush()

    found = await find_saved_views(db_session, user, FindSavedViewsArgs())
    assert found["count"] == 2
    found_p = await find_saved_views(db_session, user, FindSavedViewsArgs(page="p"))
    assert found_p["count"] == 1
    counted = await count_saved_views(db_session, user, CountSavedViewsArgs())
    assert counted["count"] == 2
