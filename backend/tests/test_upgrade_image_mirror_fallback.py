"""#787 — the download handler falls back to local FS when the mirror misses.

The appliance turns the slot-image mirror on at PROMOTE (the supervisor
derives ``slotImageMirror.enabled`` from the control-plane size), so an
image staged while the box was still single-node sits on the seed's
hostPath while the mirror's PVC comes up empty. Without a fallback the
first multi-node upgrade after a promote fails on a download the operator
cannot diagnose — the bytes are right there on the node.

The fallback is deliberately narrow, and these tests pin both halves:

* a mirror **404** falls through to local disk when the file is there;
* a mirror **502** does not, because serving a local copy would mask an
  unreachable or erroring mirror on whichever replica happens to hold a
  stale file.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException, status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.appliance import upgrade_images
from app.models.appliance import ApplianceUpgradeImage

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/appliance/upgrade-images"
_BYTES = b"\xfd7zXZ\x00fake-slot-image-payload"


async def _seed_image(db: AsyncSession) -> ApplianceUpgradeImage:
    image = ApplianceUpgradeImage(
        id=uuid.uuid4(),
        filename="spatiumddi-appliance-slot-2026.08.01-1-amd64.raw.xz",
        size_bytes=len(_BYTES),
        sha256="b" * 64,
        appliance_version="2026.08.01-1",
    )
    db.add(image)
    await db.flush()
    return image


def _stage_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, image_id: uuid.UUID) -> Path:
    """Put real bytes where the pre-promote hostPath would have them."""
    path = tmp_path / f"{image_id}.raw.xz"
    path.write_bytes(_BYTES)
    monkeypatch.setattr(upgrade_images, "_image_path", lambda _id: tmp_path / f"{_id}.raw.xz")
    return path


def _mirror_raises(monkeypatch: pytest.MonkeyPatch, exc: HTTPException) -> None:
    monkeypatch.setattr(upgrade_images.settings, "slot_image_mirror_url", "http://mirror")

    async def _boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(upgrade_images, "_stream_download_from_mirror", _boom)


def _url(image: ApplianceUpgradeImage) -> str:
    token = upgrade_images.slot_image_download_token(image.id)
    return f"{_BASE}/{image.id}/raw.xz?t={token}"


async def test_mirror_miss_serves_the_pre_promote_local_copy(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = await _seed_image(db_session)
    _stage_local(monkeypatch, tmp_path, image.id)
    _mirror_raises(
        monkeypatch,
        HTTPException(status.HTTP_404_NOT_FOUND, "Upgrade image bytes missing on mirror"),
    )

    resp = await client.get(_url(image))

    assert resp.status_code == 200
    assert resp.content == _BYTES


async def test_mirror_miss_with_no_local_copy_still_404s(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Every node that did NOT serve the original upload lands here — the
    # fallback must not invent a different outcome for them.
    image = await _seed_image(db_session)
    monkeypatch.setattr(upgrade_images, "_image_path", lambda _id: tmp_path / f"{_id}.raw.xz")
    _mirror_raises(
        monkeypatch,
        HTTPException(status.HTTP_404_NOT_FOUND, "Upgrade image bytes missing on mirror"),
    )

    resp = await client.get(_url(image))

    assert resp.status_code == 404


async def test_mirror_transport_error_is_not_masked_by_a_local_copy(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A 502 means the mirror is broken, not that the image is absent from
    # it. Falling back here would let a whole fleet upgrade off stale local
    # copies while the real store is down and nobody is told.
    image = await _seed_image(db_session)
    _stage_local(monkeypatch, tmp_path, image.id)
    _mirror_raises(
        monkeypatch,
        HTTPException(status.HTTP_502_BAD_GATEWAY, "Mirror download failed: boom"),
    )

    resp = await client.get(_url(image))

    assert resp.status_code == 502


async def test_no_mirror_configured_still_serves_local_directly(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The single-node path is untouched by the fallback branch.
    image = await _seed_image(db_session)
    _stage_local(monkeypatch, tmp_path, image.id)
    monkeypatch.setattr(upgrade_images.settings, "slot_image_mirror_url", "")

    resp = await client.get(_url(image))

    assert resp.status_code == 200
    assert resp.content == _BYTES


async def test_fallback_does_not_bypass_the_download_token(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Auth is checked before either storage path is consulted; the new
    # branch must not become a way in without a valid ``?t=``.
    image = await _seed_image(db_session)
    _stage_local(monkeypatch, tmp_path, image.id)
    _mirror_raises(
        monkeypatch,
        HTTPException(status.HTTP_404_NOT_FOUND, "Upgrade image bytes missing on mirror"),
    )

    assert (await client.get(f"{_BASE}/{image.id}/raw.xz")).status_code == 401
    assert (await client.get(f"{_BASE}/{image.id}/raw.xz?t=nope")).status_code == 403
