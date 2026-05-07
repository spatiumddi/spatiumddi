"""Backup + factory-reset read tools for the Operator Copilot
(issues #117 + #116).

Three read-only tools, all superadmin-only because the surface
exposes destination configs (even with secrets redacted, the
target name + kind + path/bucket/host is sensitive metadata):

* ``list_backup_targets`` — every configured target with last-run
  state, schedule, retention. The first thing an operator asks
  via copilot: "did all my backups succeed last night?" / "which
  targets are scheduled?".
* ``list_backup_archives_at_target`` — what's actually stored at
  a specific target right now. Calls the driver's
  ``list_archives`` so the answer matches the Backup admin
  page's "Archives" drawer exactly.
* ``find_backup_audit_history`` — timeline of backup-created /
  backup-target-run / backup-restored / factory-reset-performed
  audit rows with their per-row counters / sizes / errors.
  Useful for "when did backup last fail?" or "show me last
  week's reset history".

Deliberately NO ``propose_*`` write tools. The factory-reset and
restore paths are password-gated + confirm-phrase-gated by design;
inserting an LLM intermediary into "should I restore?" adds
friction without value, and ``propose_create_backup_target``
involves pasting destination credentials, which doesn't fit a
chat-driven flow. Operators reach for the Backup admin page when
they're about to mutate state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.auth import User
from app.models.backup import BackupTarget
from app.services.ai.tools.base import register_tool


def _superadmin_gate(user: User) -> dict[str, Any] | None:
    """Returns an error dict the caller bubbles up if not
    superadmin; ``None`` when the call is allowed. Mirrors the
    pattern from ``tools/admin.py`` for the RBAC tools.
    """
    if not user.is_superadmin:
        return {
            "error": (
                "Backup tools expose destination metadata + audit "
                "history and are restricted to superadmin users. "
                "Ask your platform admin to run the query."
            )
        }
    return None


def _try_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


# ── list_backup_targets ───────────────────────────────────────────────


class ListBackupTargetsArgs(BaseModel):
    enabled_only: bool = Field(
        default=False,
        description="When True, exclude disabled targets from the result.",
    )
    kind: str | None = Field(
        default=None,
        description=(
            "Filter by destination kind: local_volume / s3 / scp / "
            "azure_blob / smb / ftp / gcs / webdav."
        ),
    )
    last_run_status: Literal["never", "in_progress", "success", "failed"] | None = Field(
        default=None,
        description=(
            "Filter by the most recent run's status. 'failed' is the "
            "useful one — 'show me targets where last night's run "
            "failed'."
        ),
    )


@register_tool(
    name="list_backup_targets",
    description=(
        "List all configured backup targets (superadmin only). "
        "Each row carries id / name / kind / enabled / "
        "schedule_cron / retention / last_run_status / last_run_at "
        "/ last_run_filename / last_run_bytes / last_run_error / "
        "next_run_at. Use for 'did all my backups succeed last "
        "night?', 'which targets are scheduled?', or 'list failed "
        "backup runs'. Destination credentials are NOT returned; "
        "the ``config`` blob is omitted."
    ),
    args_model=ListBackupTargetsArgs,
    category="admin",
)
async def list_backup_targets(
    db: AsyncSession, user: User, args: ListBackupTargetsArgs
) -> list[dict[str, Any]]:
    gate = _superadmin_gate(user)
    if gate:
        return [gate]
    stmt = select(BackupTarget).order_by(BackupTarget.name.asc())
    if args.enabled_only:
        stmt = stmt.where(BackupTarget.enabled.is_(True))
    if args.kind:
        stmt = stmt.where(BackupTarget.kind == args.kind)
    if args.last_run_status:
        stmt = stmt.where(BackupTarget.last_run_status == args.last_run_status)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "kind": t.kind,
            "enabled": t.enabled,
            "schedule_cron": t.schedule_cron,
            "retention_keep_last_n": t.retention_keep_last_n,
            "retention_keep_days": t.retention_keep_days,
            "last_run_status": t.last_run_status,
            "last_run_at": (t.last_run_at.isoformat() if t.last_run_at else None),
            "last_run_filename": t.last_run_filename,
            "last_run_bytes": t.last_run_bytes,
            "last_run_duration_ms": t.last_run_duration_ms,
            "last_run_error": t.last_run_error,
            "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
            "passphrase_set": bool(t.passphrase_encrypted),
        }
        for t in rows
    ]


# ── list_backup_archives_at_target ────────────────────────────────────


class ListBackupArchivesArgs(BaseModel):
    target: str = Field(
        ...,
        description="Target id (UUID) or name. Names are matched exactly.",
    )
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_backup_archives_at_target",
    description=(
        "List archives currently stored at a specific backup target "
        "(superadmin only). Calls the driver's ``list_archives`` so "
        "the result matches what the Backup admin page's Archives "
        "drawer shows. Returns filename / size_bytes / created_at "
        "newest-first. Use for 'what's on my S3 bucket?' or 'how "
        "many archives are at the corp-vault target?'. Errors from "
        "the destination (auth failed, bucket not found, etc.) are "
        "returned in the response rather than raised."
    ),
    args_model=ListBackupArchivesArgs,
    category="admin",
)
async def list_backup_archives_at_target(
    db: AsyncSession, user: User, args: ListBackupArchivesArgs
) -> dict[str, Any]:
    gate = _superadmin_gate(user)
    if gate:
        return gate
    # Resolve target by id-or-name. Operators using the LLM are more
    # likely to type the name; the id path stays available for
    # programmatic callers.
    stmt = select(BackupTarget)
    target_id = _try_uuid(args.target)
    if target_id is not None:
        stmt = stmt.where(BackupTarget.id == target_id)
    else:
        stmt = stmt.where(BackupTarget.name == args.target)
    target = (await db.execute(stmt)).scalar_one_or_none()
    if target is None:
        return {"error": f"backup target {args.target!r} not found (by id or name)"}

    # Lazy imports to keep the module's top-level cheap on startup.
    from app.services.backup.targets import (  # noqa: PLC0415
        BackupDestinationError,
        SecretFieldError,
        decrypt_config_secrets,
        get_destination,
    )

    try:
        driver = get_destination(target.kind)
        plain_config = decrypt_config_secrets(driver, target.config)
        archives = await driver.list_archives(config=plain_config)
    except SecretFieldError as exc:
        return {
            "error": (
                f"target {target.name!r} has a secret field that can't be "
                f"decrypted with this install's SECRET_KEY: {exc}. The "
                f"target needs to be rotated before it can be used."
            )
        }
    except BackupDestinationError as exc:
        return {
            "error": (
                f"destination at target {target.name!r} ({target.kind}) "
                f"returned an error: {exc}"
            )
        }
    return {
        "target": {
            "id": str(target.id),
            "name": target.name,
            "kind": target.kind,
        },
        "count": len(archives),
        "archives": [
            {
                "filename": a.filename,
                "size_bytes": a.size_bytes,
                "created_at": a.created_at.isoformat(),
            }
            for a in archives[: args.limit]
        ],
    }


# ── find_backup_audit_history ─────────────────────────────────────────


class FindBackupAuditHistoryArgs(BaseModel):
    since_hours: float | None = Field(
        default=24 * 7,
        description=(
            "Only include audit rows newer than N hours ago. Default "
            "= 7 days. None = no lower bound."
        ),
        ge=0.0,
    )
    actions: list[str] | None = Field(
        default=None,
        description=(
            "Filter by audit action. Useful values: backup_created, "
            "backup_target_run_success, backup_target_run_failed, "
            "backup_restored, factory_reset_performed. Default = all "
            "five backup + factory-reset action types."
        ),
    )
    limit: int = Field(default=100, ge=1, le=1000)


_DEFAULT_BACKUP_ACTIONS = (
    "backup_created",
    "backup_target_run_success",
    "backup_target_run_failed",
    "backup_restored",
    "factory_reset_performed",
)


@register_tool(
    name="find_backup_audit_history",
    description=(
        "Return the recent backup + factory-reset audit history "
        "(superadmin only). Each row carries timestamp / action / "
        "actor / resource_display / result / error_detail / "
        "new_value (operator-readable counters per audit type). Use "
        "for 'when did backup last fail?', 'show me last week's "
        "reset history', or 'who restored on Tuesday?'. Default "
        "window is the last 7 days; cap is 1000 rows."
    ),
    args_model=FindBackupAuditHistoryArgs,
    category="admin",
)
async def find_backup_audit_history(
    db: AsyncSession, user: User, args: FindBackupAuditHistoryArgs
) -> list[dict[str, Any]]:
    gate = _superadmin_gate(user)
    if gate:
        return [gate]
    actions = args.actions or list(_DEFAULT_BACKUP_ACTIONS)
    stmt = select(AuditLog).where(or_(*[AuditLog.action == a for a in actions]))
    if args.since_hours is not None:
        stmt = stmt.where(
            AuditLog.timestamp >= datetime.now(UTC) - timedelta(hours=args.since_hours)
        )
    stmt = stmt.order_by(desc(AuditLog.timestamp)).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "action": r.action,
            "actor": r.user_display_name,
            "resource_type": r.resource_type,
            "resource_display": r.resource_display,
            "result": r.result,
            "error_detail": r.error_detail,
            "new_value": r.new_value,
        }
        for r in rows
    ]
