"""Backup-target CRUD + run-now + test endpoints (issue #117
Phase 1b).

All gated to superadmin. Audit-logged on every mutation. Mounted
at ``/backup/targets`` by the parent backup router.

Phase 1b ships local-volume only; the same router serves
``s3`` / ``scp`` / ``azure_blob`` in 1c / 1d via the
:mod:`app.services.backup.targets` driver registry — the API
layer doesn't need to learn each new kind.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import attributes

from app.api.deps import DB, CurrentUser
from app.core.crypto import encrypt_str
from app.models.audit import AuditLog
from app.models.backup import BackupTarget
from app.services.backup.runner import run_backup_for_target
from app.services.backup.schedule import (
    InvalidCronExpression,
    compute_next_run,
    validate_cron,
)
from app.services.backup.targets import (
    BackupDestinationError,
    DestinationConfigError,
    SecretFieldError,
    decrypt_config_secrets,
    encrypt_config_secrets,
    get_destination,
    list_destination_kinds,
    merge_config_for_update,
    redact_config_secrets,
)

router = APIRouter()
logger = structlog.get_logger(__name__)

_VALID_KINDS = {"local_volume", "s3", "scp", "azure_blob"}


def _require_superadmin(current_user: object) -> None:
    if not getattr(current_user, "is_superadmin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Backup targets are restricted to superadmin",
        )


# ── Schemas ────────────────────────────────────────────────────────────


class BackupTargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=500)
    kind: str = Field(..., min_length=1, max_length=40)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    passphrase: str = Field(..., min_length=8, max_length=512)
    passphrase_hint: str = Field("", max_length=200)
    schedule_cron: str | None = Field(default=None, max_length=120)
    retention_keep_last_n: int | None = Field(default=None, ge=0, le=10_000)
    retention_keep_days: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("kind")
    @classmethod
    def _v_kind(cls, v: str) -> str:
        if v not in _VALID_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(_VALID_KINDS)} "
                f"(Phase 1b ships local_volume; S3 / SCP / Azure follow)"
            )
        return v


class BackupTargetUpdate(BaseModel):
    """Partial update — only the explicitly-supplied fields are
    overwritten. Passphrase has its own dedicated rotation
    semantics: pass a non-None value to rotate, omit to leave
    untouched. Empty-string passphrase is rejected at the
    validator below.
    """

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    passphrase: str | None = Field(default=None, min_length=8, max_length=512)
    passphrase_hint: str | None = None
    schedule_cron: str | None = None
    retention_keep_last_n: int | None = Field(default=None, ge=0, le=10_000)
    retention_keep_days: int | None = Field(default=None, ge=0, le=10_000)


class BackupTargetResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    kind: str
    enabled: bool
    config: dict[str, Any]
    passphrase_set: bool  # never expose the encrypted bytes
    passphrase_hint: str
    schedule_cron: str | None
    retention_keep_last_n: int | None
    retention_keep_days: int | None
    last_run_status: str
    last_run_at: datetime | None
    last_run_filename: str | None
    last_run_bytes: int | None
    last_run_duration_ms: int | None
    last_run_error: str | None
    next_run_at: datetime | None
    created_at: datetime
    modified_at: datetime


def _to_response(t: BackupTarget) -> BackupTargetResponse:
    # Redact any secret fields per the driver's ``config_fields``
    # spec — operators see ``"<set>"`` rather than the encrypted
    # ciphertext (or, worse, the cleartext if a future bug bypasses
    # encryption). The driver registry knows what's secret per kind.
    try:
        driver = get_destination(t.kind)
        safe_config = redact_config_secrets(driver, t.config)
    except DestinationConfigError:
        # Unknown kind (left over from a kind we removed?). Fall
        # back to the raw config; the kind is dead anyway.
        safe_config = t.config
    return BackupTargetResponse(
        id=t.id,
        name=t.name,
        description=t.description,
        kind=t.kind,
        enabled=t.enabled,
        config=safe_config,
        passphrase_set=bool(t.passphrase_encrypted),
        passphrase_hint=t.passphrase_hint,
        schedule_cron=t.schedule_cron,
        retention_keep_last_n=t.retention_keep_last_n,
        retention_keep_days=t.retention_keep_days,
        last_run_status=t.last_run_status,
        last_run_at=t.last_run_at,
        last_run_filename=t.last_run_filename,
        last_run_bytes=t.last_run_bytes,
        last_run_duration_ms=t.last_run_duration_ms,
        last_run_error=t.last_run_error,
        next_run_at=t.next_run_at,
        created_at=t.created_at,
        modified_at=t.modified_at,
    )


# ── Endpoints ──────────────────────────────────────────────────────────


@router.get("/kinds")
async def list_kinds(current_user: CurrentUser) -> dict[str, Any]:
    """Catalog of available destination kinds + their config-field
    descriptors. The frontend reflects on these to render the
    per-kind config form.
    """
    _require_superadmin(current_user)
    return {"kinds": list_destination_kinds()}


@router.get("", response_model=list[BackupTargetResponse])
async def list_targets(db: DB, current_user: CurrentUser) -> list[BackupTargetResponse]:
    _require_superadmin(current_user)
    rows = (await db.execute(select(BackupTarget).order_by(BackupTarget.name))).scalars().all()
    return [_to_response(r) for r in rows]


@router.get("/{target_id}", response_model=BackupTargetResponse)
async def get_target(
    target_id: uuid.UUID, db: DB, current_user: CurrentUser
) -> BackupTargetResponse:
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    return _to_response(row)


@router.post("", response_model=BackupTargetResponse, status_code=201)
async def create_target(
    body: BackupTargetCreate, db: DB, current_user: CurrentUser
) -> BackupTargetResponse:
    _require_superadmin(current_user)
    if body.retention_keep_last_n is not None and body.retention_keep_days is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "retention_keep_last_n and retention_keep_days are mutually "
                "exclusive — set exactly one (or neither for no auto-prune)"
            ),
        )

    driver = get_destination(body.kind)
    try:
        driver.validate_config(body.config)
    except DestinationConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Encrypt any ``secret=True`` fields before they hit the
    # JSONB column. Driver got plaintext for validation; storage
    # gets ciphertext.
    stored_config = encrypt_config_secrets(driver, body.config)

    next_run = None
    if body.schedule_cron is not None:
        try:
            validate_cron(body.schedule_cron)
            next_run = compute_next_run(body.schedule_cron)
        except InvalidCronExpression as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = BackupTarget(
        name=body.name,
        description=body.description,
        kind=body.kind,
        enabled=body.enabled,
        config=stored_config,
        passphrase_encrypted=encrypt_str(body.passphrase),
        passphrase_hint=body.passphrase_hint,
        schedule_cron=body.schedule_cron,
        retention_keep_last_n=body.retention_keep_last_n,
        retention_keep_days=body.retention_keep_days,
        next_run_at=next_run,
    )
    db.add(row)
    db.add(
        AuditLog(
            action="create",
            resource_type="backup_target",
            resource_id=str(row.id),
            resource_display=body.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            new_value={
                "kind": body.kind,
                "enabled": body.enabled,
                "schedule_cron": body.schedule_cron,
            },
        )
    )
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.patch("/{target_id}", response_model=BackupTargetResponse)
async def update_target(
    target_id: uuid.UUID,
    body: BackupTargetUpdate,
    db: DB,
    current_user: CurrentUser,
) -> BackupTargetResponse:
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")

    payload = body.model_dump(exclude_unset=True)

    new_keep_n = payload.get("retention_keep_last_n", row.retention_keep_last_n)
    new_keep_days = payload.get("retention_keep_days", row.retention_keep_days)
    if new_keep_n is not None and new_keep_days is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "retention_keep_last_n and retention_keep_days are mutually "
                "exclusive — set exactly one (or neither for no auto-prune)"
            ),
        )

    if "config" in payload:
        driver = get_destination(row.kind)
        # PATCH semantics for secret fields: an operator who only
        # changes the bucket name shouldn't have to retype the
        # secret access key. ``merge_config_for_update`` keeps the
        # existing encrypted value when the incoming payload omits
        # the secret (or sends the redaction sentinel). Validation
        # runs on the merged dict so shape checks see all fields.
        merged = merge_config_for_update(driver, incoming=payload["config"], existing=row.config)
        try:
            driver.validate_config(merged)
        except DestinationConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Re-encrypt — fields carried over are already wrapped
        # (the helper detects the prefix and skips them); newly
        # supplied secrets get wrapped fresh.
        row.config = encrypt_config_secrets(driver, merged)
        attributes.flag_modified(row, "config")

    if "schedule_cron" in payload:
        if payload["schedule_cron"] is None or payload["schedule_cron"] == "":
            row.schedule_cron = None
            row.next_run_at = None
        else:
            try:
                validate_cron(payload["schedule_cron"])
                row.next_run_at = compute_next_run(payload["schedule_cron"])
            except InvalidCronExpression as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            row.schedule_cron = payload["schedule_cron"]

    if "passphrase" in payload and payload["passphrase"] is not None:
        row.passphrase_encrypted = encrypt_str(payload["passphrase"])

    for key in (
        "name",
        "description",
        "enabled",
        "passphrase_hint",
        "retention_keep_last_n",
        "retention_keep_days",
    ):
        if key in payload:
            setattr(row, key, payload[key])

    db.add(
        AuditLog(
            action="update",
            resource_type="backup_target",
            resource_id=str(row.id),
            resource_display=row.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            new_value={k: v for k, v in payload.items() if k != "passphrase"},
        )
    )
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.delete("/{target_id}", status_code=204)
async def delete_target(target_id: uuid.UUID, db: DB, current_user: CurrentUser) -> None:
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    db.add(
        AuditLog(
            action="delete",
            resource_type="backup_target",
            resource_id=str(row.id),
            resource_display=row.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
        )
    )
    await db.delete(row)
    await db.commit()


# ── Run-now / test / archive listing ───────────────────────────────────


class RunNowResponse(BaseModel):
    success: bool
    filename: str | None
    bytes: int | None
    duration_ms: int | None
    deleted: int
    error: str | None


@router.post("/{target_id}/run-now", response_model=RunNowResponse)
async def run_target_now(target_id: uuid.UUID, db: DB, current_user: CurrentUser) -> RunNowResponse:
    """Kick a one-off backup against this target. Synchronous —
    blocks until ``pg_dump`` + driver write + retention prune
    finish. The schedule sweep uses the same code path.
    """
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="target is disabled — enable it first")
    result = await run_backup_for_target(
        db,
        target=row,
        triggered_by="manual",
        actor_id=current_user.id,
        actor_display=current_user.username,
    )
    return RunNowResponse(**result)


@router.post("/{target_id}/test")
async def test_target(target_id: uuid.UUID, db: DB, current_user: CurrentUser) -> dict[str, Any]:
    """Connectivity probe — write + list + delete a tiny file at
    the destination. Doesn't touch the DB or build a real
    archive.
    """
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    driver = get_destination(row.kind)
    try:
        plain_config = decrypt_config_secrets(driver, row.config)
        outcome = await driver.test_connection(config=plain_config)
    except SecretFieldError as exc:
        outcome = {"ok": False, "error": str(exc)}
    except BackupDestinationError as exc:
        outcome = {"ok": False, "error": str(exc)}
    return outcome


class ArchiveListingResponse(BaseModel):
    filename: str
    size_bytes: int
    created_at: datetime


@router.get("/{target_id}/archives", response_model=list[ArchiveListingResponse])
async def list_target_archives(
    target_id: uuid.UUID, db: DB, current_user: CurrentUser
) -> list[ArchiveListingResponse]:
    """List archives stored at this target, newest-first."""
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    driver = get_destination(row.kind)
    try:
        plain_config = decrypt_config_secrets(driver, row.config)
        archives = await driver.list_archives(config=plain_config)
    except SecretFieldError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BackupDestinationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [
        ArchiveListingResponse(
            filename=a.filename,
            size_bytes=a.size_bytes,
            created_at=a.created_at,
        )
        for a in archives
    ]


class RestoreFromArchiveBody(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    passphrase: str = Field(..., min_length=8, max_length=512)
    confirmation_phrase: str


@router.post("/{target_id}/archives/restore")
async def restore_from_archive(
    target_id: uuid.UUID,
    body: RestoreFromArchiveBody,
    db: DB,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Pull ``filename`` from the destination, decrypt + replay
    via the same code path as ``POST /backup/restore`` (Phase 1a).
    Operator types the passphrase even though the target stores
    one — symmetric with the upload-based restore + proves the
    operator knows the key, so a stolen session token can't roll
    back the install on a hunch.
    """
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    driver = get_destination(row.kind)
    try:
        plain_config = decrypt_config_secrets(driver, row.config)
        archive_bytes = await driver.download(config=plain_config, filename=body.filename)
    except SecretFieldError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BackupDestinationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not archive_bytes:
        raise HTTPException(
            status_code=502,
            detail=f"archive {body.filename!r} fetched empty from destination",
        )

    # Reuse the Phase 1a restore path so the safety dump +
    # passphrase verify + psql replay + post-replay audit row all
    # behave the same as the upload-based restore.
    from app.config import settings  # noqa: PLC0415
    from app.services.backup import (  # noqa: PLC0415
        BackupArchiveError,
        BackupCryptoError,
        BackupRestoreError,
        apply_backup_restore,
    )

    try:
        outcome = await apply_backup_restore(
            db,
            archive_bytes=archive_bytes,
            passphrase=body.passphrase,
            confirmation_phrase=body.confirmation_phrase,
            db_url=str(settings.database_url),
        )
    except (BackupArchiveError, BackupCryptoError, BackupRestoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Post-replay audit on a fresh session — the current ``db``
    # session was closed inside ``apply_backup_restore`` (engine
    # disposed). Same shape the Phase 1a upload-restore endpoint
    # uses.
    from app.db import AsyncSessionLocal  # noqa: PLC0415

    async with AsyncSessionLocal() as fresh:
        fresh.add(
            AuditLog(
                action="backup_restored",
                resource_type="backup_target",
                resource_id=str(row.id),
                resource_display=row.name,
                user_id=current_user.id,
                user_display_name=current_user.username,
                result="success",
                new_value={
                    "source": "destination",
                    "target_kind": row.kind,
                    "filename": body.filename,
                    "manifest": outcome.manifest,
                    "duration_ms": outcome.duration_ms,
                    "pre_restore_safety_path": outcome.pre_restore_path,
                },
            )
        )
        await fresh.commit()

    return {
        "success": True,
        "filename": body.filename,
        "duration_ms": outcome.duration_ms,
        "manifest": outcome.manifest,
        "pre_restore_safety_path": outcome.pre_restore_path,
    }


@router.delete("/{target_id}/archives/{filename}", status_code=204)
async def delete_target_archive(
    target_id: uuid.UUID,
    filename: str,
    db: DB,
    current_user: CurrentUser,
) -> None:
    """Manually drop one archive at this target."""
    _require_superadmin(current_user)
    row = await db.get(BackupTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="backup target not found")
    driver = get_destination(row.kind)
    try:
        plain_config = decrypt_config_secrets(driver, row.config)
        await driver.delete(config=plain_config, filename=filename)
    except SecretFieldError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BackupDestinationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.add(
        AuditLog(
            action="backup_archive_deleted",
            resource_type="backup_target",
            resource_id=str(row.id),
            resource_display=row.name,
            user_id=current_user.id,
            user_display_name=current_user.username,
            result="success",
            new_value={"filename": filename},
        )
    )
    await db.commit()
