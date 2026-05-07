"""Run a backup against one configured target (issue #117 Phase 1b).

Threads :func:`build_backup_archive` (Phase 1a) into the
destination drivers + the per-target retention sweep + the
``last_run_*`` state-machine on ``backup_target``. Used by both
the on-demand "Run Now" button and the beat-driven schedule
sweep.

State machine on the row:

* Pre-run → ``last_run_status = "in_progress"``, all other
  ``last_run_*`` cleared. Stamp committed before the destination
  write so a stuck driver can't leave the row in ambiguous state.
* On success → ``last_run_status = "success"``, filename / bytes
  / duration_ms populated, ``last_run_error = NULL``,
  ``next_run_at`` recomputed from the cron string.
* On failure → ``last_run_status = "failed"``, error captured,
  ``next_run_at`` still recomputed so the next tick retries
  rather than getting wedged.

Audit-log row written for both outcomes — same shape as the
on-demand backup endpoint emits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str
from app.models.audit import AuditLog
from app.models.backup import BackupTarget
from app.services.backup.archive import (
    BackupArchiveError,
    build_backup_archive,
)
from app.services.backup.schedule import compute_next_run
from app.services.backup.targets import (
    BackupDestinationError,
    get_destination,
)

logger = structlog.get_logger(__name__)


def _decrypt_passphrase(target: BackupTarget) -> str:
    """Pull the operator's per-target passphrase out of the
    Fernet-encrypted column. Surfaces a clean
    :class:`BackupArchiveError` if the column is somehow corrupt
    so the runner's ``except (BackupArchiveError, ...)`` catch
    grounds the failure message in something operator-readable.
    """
    try:
        return decrypt_str(target.passphrase_encrypted)
    except ValueError as exc:
        raise BackupArchiveError(
            "could not decrypt the target's stored passphrase — "
            "re-save the target with a fresh passphrase"
        ) from exc


async def _retention_sweep(
    db: AsyncSession,
    *,
    target: BackupTarget,
    config: dict[str, Any],
) -> int:
    """Drop archives outside the target's retention window. One of
    ``retention_keep_last_n`` / ``retention_keep_days`` may be set
    (mutually exclusive at the validator layer); both NULL means
    "no automatic pruning". Returns the number of archives
    deleted.
    """
    if target.retention_keep_last_n is None and target.retention_keep_days is None:
        return 0
    driver = get_destination(target.kind)
    archives = await driver.list_archives(config=config)
    deleted = 0
    if target.retention_keep_last_n is not None:
        keep_n = max(target.retention_keep_last_n, 0)
        for stale in archives[keep_n:]:
            try:
                await driver.delete(config=config, filename=stale.filename)
                deleted += 1
            except BackupDestinationError as exc:
                logger.warning(
                    "backup_retention_delete_failed",
                    target_id=str(target.id),
                    filename=stale.filename,
                    error=str(exc),
                )
    elif target.retention_keep_days is not None:
        cutoff = datetime.now(UTC).timestamp() - target.retention_keep_days * 86400
        for archive in archives:
            if archive.created_at.timestamp() < cutoff:
                try:
                    await driver.delete(config=config, filename=archive.filename)
                    deleted += 1
                except BackupDestinationError as exc:
                    logger.warning(
                        "backup_retention_delete_failed",
                        target_id=str(target.id),
                        filename=archive.filename,
                        error=str(exc),
                    )
    return deleted


async def run_backup_for_target(
    db: AsyncSession,
    *,
    target: BackupTarget,
    triggered_by: str,
    actor_id: Any | None = None,
    actor_display: str = "system",
) -> dict[str, Any]:
    """Build + write + prune for one target. Caller passes the
    fully-loaded ``target`` row + a label for ``triggered_by``
    (``"manual"`` / ``"schedule"``) which lands in the audit row.

    Returns a result dict the caller can render straight to the
    UI: ``{success, filename, bytes, duration_ms, error, deleted}``.
    """
    started = datetime.now(UTC)
    target.last_run_status = "in_progress"
    target.last_run_at = started
    target.last_run_filename = None
    target.last_run_bytes = None
    target.last_run_duration_ms = None
    target.last_run_error = None
    await db.commit()
    await db.refresh(target)

    result: dict[str, Any] = {
        "success": False,
        "filename": None,
        "bytes": None,
        "duration_ms": None,
        "error": None,
        "deleted": 0,
    }

    try:
        passphrase = _decrypt_passphrase(target)
        driver = get_destination(target.kind)
        driver.validate_config(target.config)

        archive_bytes, filename = await build_backup_archive(
            db,
            passphrase=passphrase,
            passphrase_hint=target.passphrase_hint or None,
        )
        await driver.write(config=target.config, filename=filename, archive_bytes=archive_bytes)
        deleted = await _retention_sweep(db, target=target, config=target.config)

        finished = datetime.now(UTC)
        duration_ms = int((finished - started).total_seconds() * 1000)
        target.last_run_status = "success"
        target.last_run_filename = filename
        target.last_run_bytes = len(archive_bytes)
        target.last_run_duration_ms = duration_ms
        if target.schedule_cron:
            target.next_run_at = compute_next_run(target.schedule_cron, after=finished)
        result.update(
            {
                "success": True,
                "filename": filename,
                "bytes": len(archive_bytes),
                "duration_ms": duration_ms,
                "deleted": deleted,
            }
        )
        action = "backup_target_run_success"
        result_state = "success"
    except (BackupArchiveError, BackupDestinationError) as exc:
        finished = datetime.now(UTC)
        duration_ms = int((finished - started).total_seconds() * 1000)
        target.last_run_status = "failed"
        target.last_run_duration_ms = duration_ms
        target.last_run_error = str(exc)[:5000]
        if target.schedule_cron:
            target.next_run_at = compute_next_run(target.schedule_cron, after=finished)
        result.update({"error": str(exc), "duration_ms": duration_ms})
        action = "backup_target_run_failed"
        result_state = "failed"
        logger.warning(
            "backup_target_run_failed",
            target_id=str(target.id),
            kind=target.kind,
            error=str(exc),
        )

    db.add(
        AuditLog(
            action=action,
            resource_type="backup_target",
            resource_id=str(target.id),
            resource_display=target.name,
            user_id=actor_id,
            user_display_name=actor_display,
            result=result_state,
            new_value={
                "triggered_by": triggered_by,
                "kind": target.kind,
                **{k: v for k, v in result.items() if k != "error" or v is not None},
            },
            error_detail=result.get("error"),
        )
    )
    await db.commit()
    await db.refresh(target)
    return result
