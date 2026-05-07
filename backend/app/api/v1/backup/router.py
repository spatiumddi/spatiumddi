"""Backup + restore endpoints (issue #117 Phase 1a).

Two endpoints today, both superadmin-only:

* ``POST /backup/create-and-download`` — synchronous: build the
  archive in memory, stream it back via ``Content-Disposition:
  attachment``. Same shape as the conformity PDF export.
* ``POST /backup/restore`` — multipart upload (zip file +
  passphrase + confirmation phrase). Validates the archive, takes
  a pre-restore safety dump, replays the SQL via
  ``psql --single-transaction``, returns a JSON outcome summary.

Phase 1b will add backup-target rows + scheduled remote
destinations (S3 / SCP / Azure / etc.) under the same router
prefix.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import DB, CurrentUser
from app.config import settings
from app.models.audit import AuditLog
from app.services.backup import (
    BackupArchiveError,
    BackupCryptoError,
    BackupRestoreError,
    apply_backup_restore,
    build_backup_archive,
)
from app.services.backup.sections import SECTIONS

router = APIRouter()
logger = structlog.get_logger(__name__)

# Hard ceiling on uploaded backup archives. SpatiumDDI installs are
# single-digit-GB at most (see Phase 1a scope notes); anything past
# 2 GB is almost certainly an accident or a malicious payload.
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


def _require_superadmin(current_user: object) -> None:
    if not getattr(current_user, "is_superadmin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Backup + restore is restricted to superadmin",
        )


# ── /backup/create-and-download ──────────────────────────────────────


@router.get("/sections")
async def list_backup_sections(current_user: CurrentUser) -> dict[str, Any]:
    """Catalog of backup sections (issue #117 Phase 2a). Drives
    the upcoming selective-backup + selective-restore checkboxes —
    operators tick which sections to include / apply.
    """
    if not getattr(current_user, "is_superadmin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Backup is restricted to superadmin",
        )
    return {
        "sections": [
            {
                "key": s.key,
                "label": s.label,
                "description": s.description,
                "table_count": len(s.tables),
                "volatile": s.volatile,
                "selectable": s.selectable,
            }
            for s in SECTIONS
        ]
    }


@router.post("/create-and-download")
async def create_and_download_backup(
    db: DB,
    current_user: CurrentUser,
    passphrase: str = Form(..., min_length=8, max_length=512),
    passphrase_hint: str = Form(default="", max_length=200),
) -> StreamingResponse:
    """Build a backup archive synchronously and stream it as a
    zip download. Operator passphrase is required (min 8 chars) so
    the secret-bearing payload inside ``secrets.enc`` is never
    written in clear.
    """
    _require_superadmin(current_user)
    try:
        archive_bytes, filename = await build_backup_archive(
            db,
            passphrase=passphrase,
            passphrase_hint=passphrase_hint,
        )
    except BackupArchiveError as exc:
        logger.warning("backup_create_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"backup failed: {exc}") from exc

    db.add(
        AuditLog(
            action="backup_created",
            resource_type="platform",
            resource_id="backup",
            resource_display=filename,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            new_value={
                "filename": filename,
                "bytes": len(archive_bytes),
                "passphrase_hint": passphrase_hint or None,
            },
        )
    )
    await db.commit()

    def _iter() -> Any:
        # Single-shot iterator — the archive lives in memory at
        # this point, we just hand it off in one chunk so the
        # browser sees Content-Length and can show a real
        # progress bar. Streaming a generator one-byte-at-a-time
        # would defeat that.
        yield archive_bytes

    return StreamingResponse(
        _iter(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(archive_bytes)),
        },
    )


# ── /backup/restore ──────────────────────────────────────────────────


class RestoreOutcomeResponse(BaseModel):
    success: bool
    pre_restore_safety_path: str | None
    duration_ms: int
    manifest: dict[str, Any]
    secrets_payload_keys: list[str]
    note: str
    selective: bool = False
    restored_sections: list[str] | None = None


@router.post("/restore", response_model=RestoreOutcomeResponse)
async def restore_backup(
    db: DB,
    current_user: CurrentUser,
    archive: UploadFile = File(...),
    passphrase: str = Form(..., min_length=8, max_length=512),
    confirmation_phrase: str = Form(...),
    sections: str = Form(default=""),
) -> RestoreOutcomeResponse:
    """Apply a backup archive — hard overwrite OR selective per
    sections. The operator must type the literal phrase
    ``RESTORE-FROM-BACKUP`` so accidental drag-and-drops don't
    nuke the install. A pre-restore safety dump is taken before
    any destructive change so botched restores have a recovery
    path on the local filesystem.

    When ``sections`` is empty (Phase 1 default) the call is a
    full restore. Pass a comma-separated list of section keys
    (from ``GET /backup/sections``) for a selective restore;
    those sections' tables are TRUNCATEd CASCADE and re-loaded
    from the archive while the rest stay untouched.
    """
    _require_superadmin(current_user)

    archive_bytes = await archive.read()
    if not archive_bytes:
        raise HTTPException(status_code=422, detail="archive is empty")
    if len(archive_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"archive exceeds {_MAX_UPLOAD_BYTES} bytes",
        )

    sections_list = [s.strip() for s in sections.split(",") if s.strip()] or None
    try:
        outcome = await apply_backup_restore(
            db,
            archive_bytes=archive_bytes,
            passphrase=passphrase,
            confirmation_phrase=confirmation_phrase,
            db_url=str(settings.database_url),
            sections=sections_list,
        )
    except (BackupArchiveError, BackupCryptoError, BackupRestoreError) as exc:
        # Same shape for archive / crypto / restore errors — the
        # message is enough for the operator to know whether to
        # re-export, retype the passphrase, or upgrade the
        # destination install. Surfacing class names would just
        # confuse non-engineers without adding info.
        logger.warning(
            "backup_restore_failed",
            error=str(exc),
            error_class=type(exc).__name__,
            archive_bytes=len(archive_bytes),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # The restore replayed the archive's SQL, which means the
    # ``audit_log`` table is now whatever was in the archive — our
    # own commit below survives because it's a NEW row inserted
    # post-replay. We deliberately log the restore *after* the
    # destructive step so the trail of evidence sits in the freshly
    # restored database, not in the wiped one.
    #
    # Re-open the session via a fresh insertion. The current ``db``
    # session was closed inside ``apply_backup_restore``; reach for
    # AsyncSessionLocal here instead.
    from app.db import AsyncSessionLocal  # noqa: PLC0415

    async with AsyncSessionLocal() as fresh:
        fresh.add(
            AuditLog(
                action="backup_restored",
                resource_type="platform",
                resource_id="backup",
                resource_display=str(outcome.manifest.get("hostname", "unknown-source")),
                user_id=current_user.id,
                user_display_name=current_user.username,
                result="success",
                new_value={
                    "manifest": outcome.manifest,
                    "pre_restore_safety_path": outcome.pre_restore_path,
                    "duration_ms": outcome.duration_ms,
                },
            )
        )
        await fresh.commit()

    note = (
        "Restore complete. Secret-bearing rows (auth provider creds, "
        "agent PSKs, integration credentials) are encrypted with the "
        "source install's SECRET_KEY. If you restored onto a different "
        "install, decrypt secrets.enc with your passphrase and apply "
        "the recovered SECRET_KEY to this install's environment, "
        "then restart the api / worker / beat containers."
    )
    if outcome.pre_restore_path is None:
        note += (
            " WARNING: pre-restore safety dump was NOT written "
            "(no writable /var/lib/spatiumddi/backups path). If you "
            "need to roll back, the only path is restoring from a "
            "prior backup."
        )

    return RestoreOutcomeResponse(
        success=True,
        pre_restore_safety_path=outcome.pre_restore_path,
        duration_ms=outcome.duration_ms,
        manifest=outcome.manifest,
        secrets_payload_keys=outcome.secrets_payload_keys,
        note=note,
        selective=outcome.selective,
        restored_sections=outcome.restored_sections,
    )


# ── /backup/manifest-preview ─────────────────────────────────────────


@router.post("/manifest-preview")
async def preview_archive_manifest(
    current_user: CurrentUser,
    archive: UploadFile = File(...),
) -> dict[str, Any]:
    """Pull just ``manifest.json`` from an uploaded archive without
    applying anything. Lets the restore UI show the operator
    "you're about to restore from <hostname> @ <created_at>" before
    they commit to the typed-confirmation step.
    """
    _require_superadmin(current_user)
    archive_bytes = await archive.read()
    if not archive_bytes:
        raise HTTPException(status_code=422, detail="archive is empty")
    if len(archive_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"archive exceeds {_MAX_UPLOAD_BYTES} bytes",
        )
    from app.services.backup.archive import (  # noqa: PLC0415
        read_backup_manifest,
    )

    try:
        manifest = read_backup_manifest(archive_bytes)
    except BackupArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "manifest": manifest,
        "archive_bytes": len(archive_bytes),
        "format_recognised": manifest.get("format") == "spatiumddi-backup",
    }
