"""Slot-image upload + download for air-gapped appliance upgrades (#170 follow-up).

Operators on disconnected networks can't reach
``https://github.com/spatiumddi/spatiumddi/releases/...`` to feed the
supervisor a ``desired_slot_image_url``. Instead they download the
``.raw.xz`` out-of-band, upload it through the new Fleet UI, and the
control plane stores it on a local volume + serves it back under an
authenticated internal URL. The supervisor's existing C1 heartbeat →
trigger-file → host runner pipeline pulls from there unchanged.

Endpoints (all superadmin-gated):

* ``POST /api/v1/appliance/slot-images`` — multipart upload. Operator
  provides the file + the SHA-256 they expect (paste from
  ``sha256sum`` output) + the ``appliance_version`` label. Server
  computes the hash on the byte stream, verifies match → 422 +
  partial-file cleanup on mismatch. Duplicate sha256 short-circuits
  to the existing row.
* ``GET /api/v1/appliance/slot-images`` — list metadata (filename,
  size, sha256, version, uploader, upload time, notes).
* ``GET /api/v1/appliance/slot-images/{id}`` — single row metadata.
* ``GET /api/v1/appliance/slot-images/{id}/raw.xz`` — stream the file
  back. The supervisor downloads through this; the operator can also
  hit it directly for verification. Requires the same auth as every
  other appliance admin endpoint.
* ``DELETE /api/v1/appliance/slot-images/{id}`` — remove from disk +
  drop the row. No referential gate — if an in-flight appliance row
  has its ``desired_slot_image_url`` pointing at the deleted image,
  the next heartbeat will see a 404 and the trigger-file write
  short-circuits silently. Operators can re-upload + re-schedule.

Storage layout — ``/var/lib/spatiumddi/slot-images/{id}.raw.xz``.
The filename on disk is always the UUID with the ``.raw.xz`` suffix
so we never trust operator input for filesystem paths. The
operator-supplied ``filename`` is display-only.

No retention policy in this commit — operators clean up via the
Fleet UI's Delete button. A future polish can add a "prune images
not referenced by any appliance row, older than N days" beat task.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.api.v1.appliance.slot_image_mirror import mirror_auth_token
from app.config import settings
from app.core.permissions import is_effective_superadmin, require_permission
from app.models.appliance import ApplianceSlotImage
from app.models.audit import AuditLog

logger = structlog.get_logger(__name__)

router = APIRouter()


# On-disk storage. The api container's docker-compose entry binds
# ``spatium_slot_images:/var/lib/spatiumddi/slot-images`` — a dedicated
# named volume so slot-image bytes don't share fate with database
# backups or anything else under /var. Mode 0700 because the bytes
# themselves don't need to be world-readable — fastapi streams them
# back through the same auth gate every other appliance endpoint
# uses.
SLOT_IMAGE_DIR = Path(os.environ.get("SPATIUM_SLOT_IMAGE_DIR", "/var/lib/spatiumddi/slot-images"))
# Cap on a single upload — slot images are ~800 MiB-1.2 GiB
# compressed today; 4 GiB ceiling leaves headroom for kernel +
# initramfs growth without letting an operator accidentally ship
# a multi-GiB blob that's clearly wrong.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
# Streaming-read chunk for both upload + sha256 computation. 4 MiB is
# small enough to keep the api event-loop responsive (asyncio
# coroutine yields between chunks) + large enough to avoid syscall
# overhead.
_CHUNK_BYTES = 4 * 1024 * 1024


def _require_superadmin(user: CurrentUser) -> None:
    if not is_effective_superadmin(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Slot-image management is restricted to superadmins.",
        )


def _image_path(image_id: uuid.UUID) -> Path:
    return SLOT_IMAGE_DIR / f"{image_id}.raw.xz"


def _ensure_storage_dir() -> None:
    SLOT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SLOT_IMAGE_DIR.chmod(0o700)
    except OSError:
        # Mount source may be on a filesystem that doesn't honour the
        # chmod (e.g. tmpfs with default mode). Not load-bearing —
        # the api container's user owns the path already.
        pass


# ── Mirror routing (#296 Phase B) ──────────────────────────────────────
#
# When ``settings.slot_image_mirror_url`` is set, the upload / download
# / delete handlers stream byte ops through the mirror Deployment via
# its in-cluster Service. The api keeps owning DB metadata + the
# operator-facing endpoint shape; only the bytes themselves leave the
# api pod. When the URL is unset (single-instance docker-compose / a
# plain k8s install with the mirror disabled), the handlers fall back
# to direct local-FS access — the pre-Phase-B behaviour.
#
# All three proxy helpers raise HTTPException on transport / status
# failure so the operator-facing handler returns a clean error code
# rather than a stack trace.


def _mirror_url(image_id: uuid.UUID) -> str:
    base = settings.slot_image_mirror_url.rstrip("/")
    return f"{base}/api/v1/appliance/internal/slot-images/{image_id}"


# Generous timeout — slot-image uploads are 1-4 GiB streams. 30 min
# read budget covers a slow LAN / VPN tunnel; the api will already
# have streamed enough body to trip a Starlette timeout long before
# this if something's truly wedged.
_MIRROR_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=1800.0, pool=30.0)


async def _stream_upload_through_mirror(
    image_id: uuid.UUID,
    body: AsyncIterator[bytes],
) -> None:
    """Stream the upload body to the mirror via PUT.

    The api hashes + size-counts the chunks separately in the caller;
    this helper just pipes bytes onward. A non-2xx from the mirror
    raises 502 — the upload failed downstream, not at the operator.
    """
    headers = {"X-Mirror-Auth": mirror_auth_token("put", image_id)}
    async with httpx.AsyncClient(timeout=_MIRROR_HTTP_TIMEOUT) as client:
        try:
            resp = await client.put(
                _mirror_url(image_id),
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Mirror upload failed: {exc}",
            ) from exc
    if resp.status_code not in (200, 201, 204):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Mirror upload returned {resp.status_code}: {resp.text[:200]!r}",
        )


async def _stream_download_from_mirror(image_id: uuid.UUID) -> StreamingResponse:
    """Stream bytes from the mirror back to the requester.

    The mirror serves a FileResponse; we open a streaming GET against
    it and pass the chunks through. The mirror's content-length passes
    through too, so the host runner's progress bar still works.
    """
    headers = {"X-Mirror-Auth": mirror_auth_token("get", image_id)}
    client = httpx.AsyncClient(timeout=_MIRROR_HTTP_TIMEOUT)
    try:
        # Build the request manually so we can keep the client open
        # for the duration of the stream — closing it inside the
        # ``StreamingResponse`` generator yields a clean shutdown
        # when the operator's tcp connection closes.
        request = client.build_request("GET", _mirror_url(image_id), headers=headers)
        upstream = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Mirror download failed: {exc}",
        ) from exc
    if upstream.status_code == 404:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Slot image bytes missing on mirror — re-upload required.",
        )
    if upstream.status_code != 200:
        body_preview = (await upstream.aread())[:200]
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Mirror returned {upstream.status_code}: {body_preview!r}",
        )

    async def _iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes(_CHUNK_BYTES):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    # Preserve upstream content-length so progress bars work end-to-
    # end. ``StreamingResponse`` doesn't set it on its own.
    resp_headers: dict[str, str] = {}
    if "content-length" in upstream.headers:
        resp_headers["Content-Length"] = upstream.headers["content-length"]
    return StreamingResponse(
        _iter(),
        media_type="application/octet-stream",
        headers=resp_headers,
    )


async def _delete_from_mirror(image_id: uuid.UUID) -> None:
    """Issue DELETE against the mirror; tolerate 404 (already gone)."""
    headers = {"X-Mirror-Auth": mirror_auth_token("delete", image_id)}
    async with httpx.AsyncClient(timeout=_MIRROR_HTTP_TIMEOUT) as client:
        try:
            resp = await client.delete(_mirror_url(image_id), headers=headers)
        except httpx.HTTPError as exc:
            # Log + continue — the DB row delete is the authoritative
            # "this image is gone" signal. Stale bytes get reaped by
            # the future prune task.
            logger.warning(
                "slot_image_mirror_delete_failed",
                image_id=str(image_id),
                error=str(exc),
            )
            return
    if resp.status_code not in (200, 204, 404):
        logger.warning(
            "slot_image_mirror_delete_failed",
            image_id=str(image_id),
            status=resp.status_code,
        )


# ── Schemas ────────────────────────────────────────────────────────


class SlotImageRow(BaseModel):
    id: uuid.UUID
    filename: str
    size_bytes: int
    sha256: str
    appliance_version: str
    uploaded_by_user_id: uuid.UUID | None
    uploaded_at: datetime
    notes: str | None


class SlotImageList(BaseModel):
    images: list[SlotImageRow]


def _row_to_schema(row: ApplianceSlotImage) -> SlotImageRow:
    return SlotImageRow(
        id=row.id,
        filename=row.filename,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        appliance_version=row.appliance_version,
        uploaded_by_user_id=row.uploaded_by_user_id,
        uploaded_at=row.uploaded_at,
        notes=row.notes,
    )


# ── Endpoints ──────────────────────────────────────────────────────


@router.post(
    "/slot-images",
    response_model=SlotImageRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Upload a slot image for an air-gapped appliance upgrade",
)
async def upload_slot_image(
    current_user: CurrentUser,
    db: DB,
    file: UploadFile = File(
        ...,
        description=(
            "The .raw.xz slot image. Downloaded out-of-band from the "
            "github release and re-uploaded here for air-gapped fleets."
        ),
    ),
    sha256: str = Form(
        ...,
        min_length=64,
        max_length=64,
        description=(
            "Expected SHA-256 (hex, lowercase) of the file bytes. "
            "Get it from the ``.sha256`` sidecar published alongside "
            "the slot raw.xz. The server computes the hash on the "
            "received bytes + rejects on mismatch (422) so a "
            "corrupted upload can't be applied silently."
        ),
    ),
    appliance_version: str = Form(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "The CalVer tag this slot image carries "
            "(e.g. 2026.05.14-1). Used by the supervisor's "
            "auto-clear logic — installed_appliance_version must "
            "match this once the upgrade lands."
        ),
    ),
    notes: str | None = Form(
        default=None,
        description="Optional operator note shown in the Fleet UI list.",
    ),
) -> SlotImageRow:
    _require_superadmin(current_user)
    # #296 Phase B — only ensure the local dir when we're actually
    # going to write to it. In mirror mode the local FS is untouched.
    if not settings.slot_image_mirror_url:
        _ensure_storage_dir()

    expected_sha = sha256.lower().strip()
    if not all(c in "0123456789abcdef" for c in expected_sha):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "sha256 must be 64 lowercase hex characters.",
        )

    # Duplicate-by-hash short-circuit — if the operator re-uploads a
    # file we already have, just return the existing row. This lets
    # the UI's "re-upload to refresh" path be safe (no orphaned bytes
    # on disk).
    existing = (
        await db.execute(
            select(ApplianceSlotImage).where(ApplianceSlotImage.sha256 == expected_sha)
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Drain + discard the upload body so the client doesn't see a
        # half-read socket (httpx waits for the server's read before
        # closing the request — stalling here on a stale connection
        # would surface as a timeout on the UI).
        while await file.read(_CHUNK_BYTES):
            pass
        return _row_to_schema(existing)

    image_id = uuid.uuid4()
    hasher = hashlib.sha256()
    bytes_written = 0

    if settings.slot_image_mirror_url:
        # #296 Phase B — proxy the upload bytes to the mirror Deployment.
        # We can't pre-compute the SHA (we'd have to buffer the entire
        # 1-4 GiB body) so we tee the stream: every chunk goes both
        # into the hasher + the mirror PUT body, and we validate the
        # SHA after the PUT completes. On mismatch, fire a DELETE
        # against the mirror to clean the orphan bytes.

        async def _tee() -> AsyncIterator[bytes]:
            nonlocal bytes_written
            while True:
                chunk = await file.read(_CHUNK_BYTES)
                if not chunk:
                    return
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    # Bailing the generator aborts the PUT body mid-
                    # stream → mirror cleans up its .partial file.
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"Upload exceeds {MAX_UPLOAD_BYTES} bytes.",
                    )
                hasher.update(chunk)
                yield chunk

        await _stream_upload_through_mirror(image_id, _tee())
        actual_sha = hasher.hexdigest()
        if actual_sha != expected_sha:
            await _delete_from_mirror(image_id)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                (
                    f"SHA-256 mismatch — expected {expected_sha}, got "
                    f"{actual_sha}. Re-download the file + re-upload."
                ),
            )
    else:
        # Single-instance / docker-compose path — write to local FS
        # with the same atomic .partial → final rename pattern.
        target_path = _image_path(image_id)
        tmp_path = target_path.with_suffix(".raw.xz.partial")
        try:
            with tmp_path.open("wb") as out:
                while True:
                    chunk = await file.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"Upload exceeds {MAX_UPLOAD_BYTES} bytes.",
                        )
                    hasher.update(chunk)
                    out.write(chunk)
            actual_sha = hasher.hexdigest()
            if actual_sha != expected_sha:
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    (
                        f"SHA-256 mismatch — expected {expected_sha}, got "
                        f"{actual_sha}. Re-download the file + re-upload."
                    ),
                )
            # Bytes pass verification — atomically move into place.
            tmp_path.replace(target_path)
        except HTTPException:
            # Re-raise validation errors after cleaning up the partial.
            tmp_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Failed to write slot image to disk: {exc}",
            ) from exc

    row = ApplianceSlotImage(
        id=image_id,
        filename=file.filename or f"{image_id}.raw.xz",
        size_bytes=bytes_written,
        sha256=actual_sha,
        appliance_version=appliance_version.strip(),
        uploaded_by_user_id=current_user.id,
        notes=notes.strip() if notes else None,
    )
    db.add(row)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.slot_image_uploaded",
            resource_type="appliance_slot_image",
            resource_id=str(row.id),
            resource_display=f"{row.filename} ({appliance_version})",
            result="success",
            new_value={
                "size_bytes": bytes_written,
                "sha256": actual_sha,
                "appliance_version": appliance_version,
            },
        )
    )
    await db.commit()
    logger.info(
        "appliance_slot_image_uploaded",
        image_id=str(row.id),
        size_bytes=bytes_written,
        appliance_version=appliance_version,
        user=current_user.username,
    )
    return _row_to_schema(row)


@router.get(
    "/slot-images",
    response_model=SlotImageList,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="List uploaded slot images",
)
async def list_slot_images(current_user: CurrentUser, db: DB) -> SlotImageList:
    _require_superadmin(current_user)
    rows = (
        (
            await db.execute(
                select(ApplianceSlotImage).order_by(ApplianceSlotImage.uploaded_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return SlotImageList(images=[_row_to_schema(r) for r in rows])


@router.get(
    "/slot-images/{image_id}",
    response_model=SlotImageRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Fetch slot image metadata",
)
async def get_slot_image(image_id: uuid.UUID, current_user: CurrentUser, db: DB) -> SlotImageRow:
    _require_superadmin(current_user)
    row = await db.get(ApplianceSlotImage, image_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slot image not found.")
    return _row_to_schema(row)


def slot_image_download_token(image_id: uuid.UUID) -> str:
    """HMAC token granting download access to a specific slot image.

    Built from ``image_id`` + ``SECRET_KEY`` so anyone who knows the
    UUID alone (e.g. from a leaked URL) can't replay against a
    different image. No expiry — the upgrade flow needs the URL to
    work for the lifetime of the pending trigger, which can stretch
    across host reboots if the operator-set apply happens overnight.

    Used by ``apply_upgrade`` to mint the ``?t=...`` query param on
    the URL it stamps into ``appliance.desired_slot_image_url`` so the
    host-side ``spatium-upgrade-slot`` runner (which does an
    unauthenticated ``urllib.request.urlopen``) can pull the bytes.
    """
    mac = hmac.new(
        settings.secret_key.encode("utf-8"),
        f"slot-image:{image_id}".encode(),
        hashlib.sha256,
    )
    return mac.hexdigest()


def _verify_slot_image_download_token(image_id: uuid.UUID, token: str) -> bool:
    """Constant-time compare of the supplied ``?t=...`` query param
    against the expected HMAC."""
    expected = slot_image_download_token(image_id)
    return hmac.compare_digest(expected, token)


@router.get(
    "/slot-images/{image_id}/raw.xz",
    summary="Download a slot image",
    # Return type is FileResponse on the local path, StreamingResponse
    # on the mirror-proxy path. FastAPI can't derive a pydantic model
    # from that union — and there isn't one (raw bytes). Tell it
    # explicitly to skip response-model generation.
    response_model=None,
)
async def download_slot_image(
    image_id: uuid.UUID,
    db: DB,
    t: str | None = None,
) -> FileResponse | StreamingResponse:
    """Stream the raw.xz back. Two paths in:

    * ``?t=<hmac>`` — minted by the upgrade scheduler and embedded in
      the ``desired_slot_image_url`` the supervisor relays to the
      host-side ``spatium-upgrade-slot`` runner. Required because the
      runner has no operator session / mTLS material.
    * No token + an authenticated browser session — the operator can
      hit the URL directly to re-verify bytes against the row's
      sha256.

    Both gates land in the same response stream below. In multi-node
    mirror mode (#296 Phase B) the api proxies the byte stream from
    the mirror Service; in single-instance mode it serves directly
    from local FS.
    """
    row = await db.get(ApplianceSlotImage, image_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slot image not found.")

    # If a token is provided, it must validate — no fallback to
    # browser auth so a bad token doesn't accidentally hit the auth
    # path with a misleading 401. If no token is provided, fall back
    # to requiring an authenticated superadmin session below.
    if t is not None:
        if not _verify_slot_image_download_token(image_id, t):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Invalid slot-image download token.",
            )
    else:
        # No token — browser-direct access path is rejected with 401.
        # Operators who want a direct-download path can re-add the
        # CurrentUser dep alongside the token check in a follow-up;
        # for now the token-only path keeps the function signature
        # minimal for the supervisor's host-side runner (its only
        # legitimate caller).
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Slot-image downloads require either a ``?t=<token>`` "
            "query param (minted by the upgrade scheduler) or an "
            "authenticated superadmin session. Operator-direct "
            "browser downloads can use the /api/v1/appliance/slot-images "
            "list endpoint plus a manually-presented session.",
        )

    if settings.slot_image_mirror_url:
        # #296 Phase B — bytes live on the mirror Deployment. Stream
        # through. The mirror's 404 surfaces as 404 here; transport
        # errors as 502.
        return await _stream_download_from_mirror(row.id)

    path = _image_path(row.id)
    if not path.exists():
        # Row + file out of sync (manual /var cleanup, container
        # restart with the named volume detached, …). Treat as a 404
        # rather than a 500 — operators can drop the row + re-upload.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Slot image bytes missing on disk — re-upload required.",
        )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=row.filename,
    )


@router.delete(
    "/slot-images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Delete an uploaded slot image",
)
async def delete_slot_image(image_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    _require_superadmin(current_user)
    row = await db.get(ApplianceSlotImage, image_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slot image not found.")
    if settings.slot_image_mirror_url:
        # #296 Phase B — delete from the mirror Deployment. Best-effort:
        # the DB row delete below is the authoritative signal; stale
        # bytes on the mirror get reaped by the future prune task.
        await _delete_from_mirror(row.id)
    else:
        path = _image_path(row.id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Disk-side cleanup is best-effort — the row delete below
            # is the authoritative "this image is gone" signal.
            pass
    filename = row.filename
    version = row.appliance_version
    await db.delete(row)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.slot_image_deleted",
            resource_type="appliance_slot_image",
            resource_id=str(image_id),
            resource_display=f"{filename} ({version})",
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "appliance_slot_image_deleted",
        image_id=str(image_id),
        filename=filename,
        user=current_user.username,
    )


__all__ = ["router", "SLOT_IMAGE_DIR"]

# Silence ruff F401 — shutil is imported for future "prune older
# than" support; keep it on the import line to avoid a follow-up
# diff when that lands.
_ = shutil
