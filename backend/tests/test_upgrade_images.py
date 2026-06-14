"""Upgrade-image management — rename + GitHub import (#199).

Covers the renamed ``/api/v1/appliance/upgrade-images/*`` surface
(upload / list / delete + the ``ApplianceUpgradeImage`` table), the new
GitHub-Releases discovery + import endpoints, the asset-picking logic in
the releases service, the legacy ``/slot-images`` 308 redirect shim, and
the two new MCP read tools.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.appliance import upgrade_images
from app.core.security import create_access_token, hash_password
from app.models.appliance import ApplianceUpgradeImage
from app.models.auth import User
from app.services.ai.tools.appliance import (
    FindAvailableUpgradeImagesArgs,
    FindUpgradeImagesArgs,
    find_available_upgrade_images,
    find_upgrade_images,
)
from app.services.appliance import releases as releases_service

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/appliance/upgrade-images"


async def _token(db: AsyncSession, *, superadmin: bool = True, username: str = "uiadmin") -> str:
    user = await _user(db, superadmin=superadmin, username=username)
    return create_access_token(str(user.id))


async def _user(db: AsyncSession, *, superadmin: bool = True, username: str = "uiu") -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=superadmin,
    )
    db.add(user)
    await db.flush()
    return user


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _release(tag: str, *, assets: list[dict]) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "prerelease": False,
        "published_at": "2026-05-14T00:00:00Z",
        "html_url": f"https://github.com/spatiumddi/spatiumddi/releases/tag/{tag}",
        "assets": assets,
    }


def _image_assets(tag: str | None = None) -> list[dict]:
    stem = f"spatiumddi-appliance-slot-{tag}-amd64" if tag else "spatiumddi-appliance-slot-amd64"
    return [
        {"name": f"{stem}.raw.xz", "browser_download_url": f"https://x/{stem}.raw.xz", "size": 42},
        {"name": f"{stem}.sha256", "browser_download_url": f"https://x/{stem}.sha256"},
    ]


# ── releases-service asset logic (pure / monkeypatched fetch) ────────


def test_pick_upgrade_assets_matches_pair() -> None:
    picked = releases_service._pick_upgrade_assets(_image_assets("2026.05.14-1"))
    assert picked is not None
    image_url, sha_url, size = picked
    assert image_url.endswith("-2026.05.14-1-amd64.raw.xz")
    assert sha_url.endswith("-2026.05.14-1-amd64.sha256")
    assert size == 42


def test_pick_upgrade_assets_missing_sha_returns_none() -> None:
    assets = [
        {
            "name": "spatiumddi-appliance-slot-amd64.raw.xz",
            "browser_download_url": "https://x/raw",
        }
    ]
    assert releases_service._pick_upgrade_assets(assets) is None


def test_pick_upgrade_assets_prefers_versioned() -> None:
    assets = _image_assets() + _image_assets("2026.05.14-1")
    picked = releases_service._pick_upgrade_assets(assets)
    assert picked is not None
    # The versioned (longer) name wins.
    assert "-2026.05.14-1-amd64.raw.xz" in picked[0]


def test_partial_path_does_not_double_suffix() -> None:
    # #415 regression: with_suffix(".raw.xz.partial") replaces only the
    # ``.xz`` of ``<id>.raw.xz`` and doubles the ``.raw`` into the bogus
    # ``<id>.raw.raw.xz.partial``. _partial_path appends instead.
    img = uuid.UUID("c1ce67bb-e165-44a0-a2fd-1f03b3abcb5f")
    partial = upgrade_images._partial_path(img)
    assert partial.name == f"{img}.raw.xz.partial"
    assert ".raw.raw" not in partial.name
    # the atomic-rename target is the un-suffixed image path
    assert upgrade_images._image_path(img).name == f"{img}.raw.xz"


async def test_list_available_upgrade_images_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [
        _release("2026.05.14-1", assets=_image_assets("2026.05.14-1")),
        _release("2026.05.10-1", assets=[]),  # no upgrade-image assets → filtered out
    ]

    async def fake() -> list[dict]:
        return raw

    monkeypatch.setattr(releases_service, "_fetch_raw_releases", fake)
    reachable, rows = await releases_service.list_available_upgrade_images()
    assert reachable is True
    assert [r.tag for r in rows] == ["2026.05.14-1"]
    assert rows[0].checksum_asset_url.endswith(".sha256")


async def test_list_available_upgrade_images_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake() -> list[dict] | None:
        return None

    monkeypatch.setattr(releases_service, "_fetch_raw_releases", fake)
    reachable, rows = await releases_service.list_available_upgrade_images()
    assert reachable is False
    assert rows == []


async def test_get_upgrade_image_assets_by_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [_release("2026.05.14-1", assets=_image_assets())]

    async def fake() -> list[dict]:
        return raw

    monkeypatch.setattr(releases_service, "_fetch_raw_releases", fake)
    spec = await releases_service.get_upgrade_image_assets("2026.05.14-1")
    assert spec is not None
    assert spec.image_asset_url.endswith(".raw.xz")
    assert await releases_service.get_upgrade_image_assets("nope-9.9.9") is None


# ── upload / list / delete round-trip (renamed surface + table) ──────


async def test_upload_list_delete_roundtrip(
    db_session: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(upgrade_images, "SLOT_IMAGE_DIR", tmp_path)
    token = await _token(db_session)
    content = b"fake-raw-xz-bytes"
    sha = hashlib.sha256(content).hexdigest()

    resp = await client.post(
        _BASE,
        headers=_hdr(token),
        files={"file": ("img.raw.xz", content, "application/octet-stream")},
        data={"sha256": sha, "appliance_version": "2026.05.14-1", "notes": "rc1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sha256"] == sha
    assert body["appliance_version"] == "2026.05.14-1"
    assert body["size_bytes"] == len(content)
    image_id = body["id"]
    assert (tmp_path / f"{image_id}.raw.xz").exists()

    listed = await client.get(_BASE, headers=_hdr(token))
    assert listed.status_code == 200
    assert any(i["id"] == image_id for i in listed.json()["images"])

    deleted = await client.delete(f"{_BASE}/{image_id}", headers=_hdr(token))
    assert deleted.status_code == 204
    assert not (tmp_path / f"{image_id}.raw.xz").exists()
    assert (await client.get(_BASE, headers=_hdr(token))).json()["images"] == []


async def test_upload_sha_mismatch_422(
    db_session: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(upgrade_images, "SLOT_IMAGE_DIR", tmp_path)
    token = await _token(db_session)
    resp = await client.post(
        _BASE,
        headers=_hdr(token),
        files={"file": ("img.raw.xz", b"abc", "application/octet-stream")},
        data={"sha256": "0" * 64, "appliance_version": "2026.05.14-1"},
    )
    assert resp.status_code == 422


async def test_non_superadmin_403(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(db_session, superadmin=False, username="plain")
    resp = await client.get(_BASE, headers=_hdr(token))
    assert resp.status_code == 403


# ── available endpoint ───────────────────────────────────────────────


async def test_available_endpoint(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = releases_service.UpgradeImageRelease(
        tag="2026.05.14-1",
        name="2026.05.14-1",
        published_at=datetime(2026, 5, 14, tzinfo=UTC),
        body="",
        html_url="https://example/r",
        is_prerelease=False,
        is_installed=False,
        image_asset_url="https://x/r.raw.xz",
        checksum_asset_url="https://x/r.sha256",
        size_bytes=10,
    )

    async def fake() -> tuple[bool, list]:
        return (True, [row])

    monkeypatch.setattr(releases_service, "list_available_upgrade_images", fake)
    token = await _token(db_session)
    resp = await client.get(f"{_BASE}/available", headers=_hdr(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["github_reachable"] is True
    assert body["available"][0]["tag"] == "2026.05.14-1"
    assert body["available"][0]["image_asset_url"] == "https://x/r.raw.xz"


# ── import-from-github (404 when a tag has no importable assets) ──────


async def test_import_from_github_404_no_assets(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(tag: str):
        return None

    monkeypatch.setattr(releases_service, "get_upgrade_image_assets", fake)
    token = await _token(db_session)
    resp = await client.post(
        f"{_BASE}/import-from-github",
        headers=_hdr(token),
        json={"release_tag": "2026.05.14-1"},
    )
    assert resp.status_code == 404


# ── legacy /slot-images 308 redirect shim ────────────────────────────


async def test_slot_images_shim_redirects_308(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db_session)
    # Collection GET → 308 to /upgrade-images (httpx does not follow by default).
    resp = await client.get("/api/v1/appliance/slot-images", headers=_hdr(token))
    assert resp.status_code == 308
    assert resp.headers["location"] == "/api/v1/appliance/upgrade-images"
    # Download GET preserves the ?t= token in the redirect target.
    img = uuid.uuid4()
    resp2 = await client.get(f"/api/v1/appliance/slot-images/{img}/raw.xz?t=abc123")
    assert resp2.status_code == 308
    assert resp2.headers["location"] == f"/api/v1/appliance/upgrade-images/{img}/raw.xz?t=abc123"


# ── MCP tools ────────────────────────────────────────────────────────


async def test_find_upgrade_images_tool(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    db_session.add(
        ApplianceUpgradeImage(
            id=uuid.uuid4(),
            filename="img.raw.xz",
            size_bytes=10,
            sha256="a" * 64,
            appliance_version="2026.05.14-1",
            notes=None,
        )
    )
    await db_session.flush()
    out = await find_upgrade_images(db_session, user, FindUpgradeImagesArgs())
    assert out["count"] == 1
    assert out["images"][0]["appliance_version"] == "2026.05.14-1"
    assert out["images"][0]["sha256_short"].startswith("aaaaaaaaaaaa")


async def test_find_available_upgrade_images_tool(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = releases_service.UpgradeImageRelease(
        tag="2026.05.14-1",
        name="2026.05.14-1",
        published_at=datetime(2026, 5, 14, tzinfo=UTC),
        body="",
        html_url="",
        is_prerelease=False,
        is_installed=False,
        image_asset_url="https://x/r.raw.xz",
        checksum_asset_url="https://x/r.sha256",
        size_bytes=10,
    )

    async def fake() -> tuple[bool, list]:
        return (True, [row])

    monkeypatch.setattr(releases_service, "list_available_upgrade_images", fake)
    user = await _user(db_session)
    out = await find_available_upgrade_images(db_session, user, FindAvailableUpgradeImagesArgs())
    assert out["github_reachable"] is True
    assert out["count"] == 1
    assert out["available"][0]["tag"] == "2026.05.14-1"
