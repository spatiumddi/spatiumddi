"""Apply a backup archive to the running install (issue #117 Phase
1a).

Phase 1a is **hard overwrite**: TRUNCATE every table, replay the
archive's ``database.sql`` via ``psql --single-transaction``,
re-validate the operator's passphrase against ``secrets.enc``.
Selective restore + per-section toggles are deferred to Phase 2.

Safety rails:

* The operator must type the confirmation phrase
  ``RESTORE-FROM-BACKUP`` server-side; restore endpoints reject
  anything else.
* A pre-restore safety dump is written to
  ``/var/lib/spatiumddi/backups/pre-restore-{ts}.zip`` before any
  destructive change touches the DB. If the apply fails for any
  reason, the pre-restore dump is the operator's recovery path.
* Manifest version checks: the destination refuses archives whose
  ``format_version`` is newer than the running build (operators
  who downgraded need to upgrade first).
* The whole apply runs inside ``psql --single-transaction`` —
  failures roll back automatically; partial restores are
  impossible.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.services.backup.archive import (
    BackupArchiveError,
    _pg_env_from_url,
    build_backup_archive,
    extract_archive_members,
)
from app.services.backup.crypto import BackupCryptoError, decrypt_secrets

logger = structlog.get_logger(__name__)

CONFIRM_PHRASE = "RESTORE-FROM-BACKUP"
SUPPORTED_FORMAT_VERSION = 1
PRE_RESTORE_DIR = Path("/var/lib/spatiumddi/backups")

# psql can run for a while on a hefty install; same envelope as
# pg_dump so the matched-pair runs are bounded together.
_PSQL_TIMEOUT_SECONDS = 30 * 60


class BackupRestoreError(Exception):
    """Restore-time failures distinct from
    ``BackupArchiveError`` (zip-shape problem) and
    ``BackupCryptoError`` (passphrase wrong)."""


@dataclass
class RestoreOutcome:
    manifest: dict[str, Any]
    pre_restore_path: str | None
    secrets_payload_keys: list[str]
    duration_ms: int


async def _terminate_other_db_connections(pg_env: dict[str, str]) -> None:
    """Kick every other connection to the target database so psql's
    DROP / TRUNCATE statements don't deadlock against the worker /
    beat / agents. Postgres won't let us terminate our own session,
    which is fine — psql itself opens a brand-new connection on the
    next call.

    Failures here are logged but non-fatal; if the pool drops are
    enough on their own (no other connections present) the replay
    proceeds normally.
    """
    full_env = {**os.environ, **pg_env}
    sql = (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid();"
    )
    proc = await asyncio.create_subprocess_exec(
        "psql",
        "--set=ON_ERROR_STOP=0",
        f"--command={sql}",
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("backup_restore_terminate_timeout")
        return
    if proc.returncode != 0:
        logger.warning(
            "backup_restore_terminate_nonzero",
            stderr=stderr.decode(errors="replace")[:300],
            stdout=stdout.decode(errors="replace")[:300],
        )


async def _run_psql(sql_path: Path, db_url: str) -> None:
    pg_env, _dbname = _pg_env_from_url(db_url)
    # Kick every other connection first so the DROP / TRUNCATE in
    # the dump's --clean preamble doesn't deadlock against the
    # worker / beat / dns-bind9 / dhcp-kea / frontend SSE polls.
    await _terminate_other_db_connections(pg_env)
    full_env = {**os.environ, **pg_env}
    cmd = [
        "psql",
        "--set=ON_ERROR_STOP=1",
        "--single-transaction",
        f"--file={sql_path}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PSQL_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise BackupRestoreError(f"psql exceeded {_PSQL_TIMEOUT_SECONDS}s timeout") from exc
    if proc.returncode != 0:
        msg = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[:1500]
        raise BackupRestoreError(f"psql failed (exit {proc.returncode}): {msg}")


async def _write_pre_restore_safety_dump(db) -> str | None:
    """Take a passphrase-less local archive of the *current* state
    before clobbering anything. The passphrase is the literal
    string ``pre-restore-safety`` — operators who need to read this
    archive use that constant. The intent is "let the operator roll
    back via a SQL replay if Phase 1a's hard-overwrite was a
    mistake," not "long-term forensic vault."
    """
    try:
        PRE_RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        logger.warning(
            "pre_restore_safety_dir_unavailable",
            path=str(PRE_RESTORE_DIR),
            error=str(exc),
        )
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_path = PRE_RESTORE_DIR / f"pre-restore-{timestamp}.zip"
    try:
        archive_bytes, _filename = await build_backup_archive(
            db,
            passphrase="pre-restore-safety",
            passphrase_hint="auto pre-restore safety dump (issue #117 Phase 1a)",
        )
        out_path.write_bytes(archive_bytes)
    except (BackupArchiveError, OSError) as exc:
        logger.warning(
            "pre_restore_safety_dump_failed",
            path=str(out_path),
            error=str(exc),
        )
        return None
    return str(out_path)


async def apply_backup_restore(
    db,
    *,
    archive_bytes: bytes,
    passphrase: str,
    confirmation_phrase: str,
    db_url: str,
) -> RestoreOutcome:
    """Validate, decrypt-check, take a safety dump, then replay the
    archive's SQL via ``psql --single-transaction``.

    The async ``db`` session is used only to pull the alembic head
    for the safety dump and to dispose of the connection pool
    cleanly before psql opens its own. The actual schema rewrite
    happens out-of-process via psql to avoid the SQLAlchemy
    connection pool fighting with the destructive replay.
    """
    if confirmation_phrase != CONFIRM_PHRASE:
        raise BackupRestoreError(f"confirmation phrase must be exactly '{CONFIRM_PHRASE}'")
    if not passphrase:
        raise BackupRestoreError("passphrase is required")

    started = datetime.now(UTC)

    # Phase 1: parse + validate the archive, fail fast if it's
    # malformed, before taking the destructive safety dump path.
    manifest, db_sql, secrets_enc = extract_archive_members(archive_bytes)
    fmt_version = manifest.get("format_version")
    if fmt_version != SUPPORTED_FORMAT_VERSION:
        raise BackupRestoreError(
            f"unsupported backup format_version: {fmt_version!r} "
            f"(this build expects {SUPPORTED_FORMAT_VERSION}). "
            f"Upgrade SpatiumDDI before restoring this archive."
        )

    # Phase 2: passphrase verify. Decrypt secrets.enc up front so
    # we fail with "wrong passphrase" before deleting anything.
    try:
        secrets_payload = decrypt_secrets(secrets_enc, passphrase=passphrase)
    except BackupCryptoError as exc:
        raise BackupRestoreError(str(exc)) from exc

    # Phase 3: pre-restore safety dump. Soft-fails — if the api
    # container can't write to ``/var/lib/spatiumddi/backups`` (no
    # mounted volume in dev compose, e.g.) we proceed with a logged
    # warning. Operators on production deployments should mount the
    # path, which is documented in the deployment guide.
    pre_restore_path = await _write_pre_restore_safety_dump(db)

    # Phase 4: dispose of SQLAlchemy's connection pool. psql opens
    # its own connection, and leaving the async pool busy stalls
    # the TRUNCATE / DROP statements emitted by pg_dump --clean —
    # we'd deadlock against the worker / beat / agents reading at
    # the same time. ``engine.dispose()`` closes every pooled
    # connection cleanly so the pool comes back empty after the
    # restore. ``_terminate_other_db_connections`` (called from
    # ``_run_psql`` below) then kicks anything still attached
    # via the worker / beat / agent containers' own engines.
    from app.db import engine as global_engine  # noqa: PLC0415

    await db.close()
    await global_engine.dispose()

    # Phase 5: replay. psql under --single-transaction either
    # commits the whole archive or rolls back; partial restores
    # are impossible by construction.
    with tempfile.TemporaryDirectory(prefix="spatium-restore-") as tmpdir:
        sql_path = Path(tmpdir) / "database.sql"
        sql_path.write_bytes(db_sql)
        await _run_psql(sql_path, db_url)

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    logger.info(
        "backup_restore_applied",
        manifest_app_version=manifest.get("app_version"),
        manifest_schema_version=manifest.get("schema_version"),
        pre_restore_path=pre_restore_path,
        duration_ms=duration_ms,
    )
    return RestoreOutcome(
        manifest=manifest,
        pre_restore_path=pre_restore_path,
        secrets_payload_keys=sorted(secrets_payload.keys()),
        duration_ms=duration_ms,
    )
