"""Alembic upgrade-on-restore (issue #117 Phase 2).

When an operator restores an archive whose ``schema_version`` is
older than the local install's expected head, the destination's
freshly-restored database is at the source's schema head — every
migration that landed between source and destination is missing.
Without this step the operator has to ``docker compose exec api
alembic upgrade head`` manually before the install boots cleanly.

The flow:

1. After the data replay phase, read the local install's expected
   head from the alembic ``ScriptDirectory`` (NOT from the database
   — the DB is now at the source's head). Single-head schemas only;
   multi-head environments aren't on the supported matrix.
2. Compare against ``manifest.schema_version``:
     - equal → no-op, ``state="up_to_date"``
     - source head is an ancestor of local head → run
       ``alembic upgrade head`` against the freshly-restored DB,
       capture the ladder of revisions that ran. ``state="upgraded"``.
     - source head not in the local script chain → operator's
       destination install is OLDER than the source. Don't run
       anything; surface ``state="incompatible_newer"`` so the
       operator knows the schema in the database is ahead of this
       install's code.
     - source head is missing from the manifest entirely → an old
       Phase 1 archive that didn't carry ``schema_version``.
       ``state="unknown"`` — operator gets a heads-up, no upgrade
       attempt.

Failures of the upgrade subprocess itself are logged and surfaced
via ``state="failed"`` + ``error`` rather than raised — the data
is in. The operator can re-run ``alembic upgrade head`` manually
once they've fixed whatever blocked the migration.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.services.backup.archive import _pg_env_from_url

logger = structlog.get_logger(__name__)

_ALEMBIC_INI_PATH = Path("/app/alembic.ini")
_ALEMBIC_TIMEOUT_SECONDS = 30 * 60

MigrationState = Literal[
    "up_to_date",
    "upgraded",
    "auto_recovered",
    "incompatible_newer",
    "unknown",
    "failed",
]


# Patterns alembic / asyncpg / psql emit when the schema is already
# at (or past) the target head but ``alembic_version`` is stale.
# Hitting one of these on ``alembic upgrade head`` after a restore
# is the canonical drift-recovery signal — we stamp head instead.
_DRIFT_ERROR_PATTERNS = (
    "DuplicateTableError",
    "DuplicateColumnError",
    "DuplicateObjectError",
    "already exists",
)


@dataclass
class MigrationOutcome:
    """Result of the alembic upgrade-on-restore pass."""

    state: MigrationState
    source_head: str | None
    local_head: str | None
    migrations_applied: list[str]
    error: str | None = None


def _local_head() -> str | None:
    """Return the local install's expected single alembic head.

    Reads from the on-disk script directory, NOT the database (the
    database is at the source's head right after restore). Returns
    None when the script directory carries multiple heads — the
    upgrade-on-restore flow doesn't try to disambiguate; operators
    on multi-head schemas resolve manually.
    """
    if not _ALEMBIC_INI_PATH.is_file():
        return None
    cfg = Config(str(_ALEMBIC_INI_PATH))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if len(heads) != 1:
        return None
    return heads[0]


def _is_ancestor(script: ScriptDirectory, ancestor: str, descendant: str) -> bool:
    """Return True when ``ancestor`` appears anywhere in the
    revision chain leading to ``descendant``. ``iterate_revisions``
    walks descendant → ancestor via ``down_revision`` links.
    """
    if ancestor == descendant:
        return True
    try:
        for rev in script.iterate_revisions(descendant, "base"):
            if rev.revision == ancestor:
                return True
    except Exception:  # noqa: BLE001
        # Unknown revision id, branched chain, etc.
        return False
    return False


def _migrations_between(script: ScriptDirectory, source: str, target: str) -> list[str]:
    """Return the ordered (oldest → newest) list of revision ids
    that will run on ``alembic upgrade head`` from ``source`` to
    ``target``. Best-effort — failure to walk falls back to an
    empty list so the outcome carries a clear "we ran upgrade but
    don't know which revisions" rather than aborting.
    """
    try:
        revs = list(script.iterate_revisions(target, source))
    except Exception:  # noqa: BLE001
        return []
    # ``iterate_revisions`` yields newest → oldest; reverse so the
    # response shows the order migrations actually run.
    return [r.revision for r in reversed(revs) if r.revision != source]


async def maybe_upgrade_after_restore(
    *,
    manifest_schema_version: str | None,
    db_url: str,
) -> MigrationOutcome:
    """Walk the alembic skew check + run ``alembic upgrade head``
    if the source is on an older head. Idempotent; safe to call
    on a same-or-newer source (no-op + diagnostic state).
    """
    local_head = _local_head()
    source_head = (manifest_schema_version or "").strip() or None

    if source_head is None:
        return MigrationOutcome(
            state="unknown",
            source_head=None,
            local_head=local_head,
            migrations_applied=[],
            error="archive manifest has no schema_version",
        )
    if local_head is None:
        return MigrationOutcome(
            state="unknown",
            source_head=source_head,
            local_head=None,
            migrations_applied=[],
            error="local alembic head is ambiguous (multiple heads or no alembic.ini)",
        )
    if source_head == local_head:
        return MigrationOutcome(
            state="up_to_date",
            source_head=source_head,
            local_head=local_head,
            migrations_applied=[],
        )

    cfg = Config(str(_ALEMBIC_INI_PATH))
    script = ScriptDirectory.from_config(cfg)

    if not _is_ancestor(script, source_head, local_head):
        # Source's head isn't in our chain → either it's newer than
        # what this build knows about, or it came from a branched /
        # forked schema. Either way, we can't safely upgrade.
        return MigrationOutcome(
            state="incompatible_newer",
            source_head=source_head,
            local_head=local_head,
            migrations_applied=[],
            error=(
                f"source schema head {source_head!r} is not an ancestor of this "
                f"install's expected head {local_head!r}. The destination install "
                "is older than the source — upgrade SpatiumDDI on this destination, "
                "then re-run the restore."
            ),
        )

    # Source is on a known older head. Run ``alembic upgrade head``.
    planned = _migrations_between(script, source_head, local_head)

    pg_env, _dbname = _pg_env_from_url(db_url)
    full_env = {**os.environ, **pg_env, "DATABASE_URL": db_url}
    cmd = [
        "alembic",
        "-c",
        str(_ALEMBIC_INI_PATH),
        "upgrade",
        "head",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_ALEMBIC_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return MigrationOutcome(
            state="failed",
            source_head=source_head,
            local_head=local_head,
            migrations_applied=[],
            error=f"alembic upgrade timed out after {_ALEMBIC_TIMEOUT_SECONDS}s",
        )

    if proc.returncode != 0:
        msg = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[:1500]
        logger.error(
            "backup_restore_alembic_upgrade_failed",
            source_head=source_head,
            local_head=local_head,
            stderr=msg,
        )

        # Drift-recovery path. The dump just restored carries the
        # source's ``alembic_version`` row, which can be stale
        # relative to the *schema* the dump emits — pg_dump
        # captures whatever DDL is present regardless of the
        # alembic_version value. Concretely: if a backup was
        # taken when alembic_version had drifted (operator ran
        # ``alembic stamp`` to fix an earlier inconsistency, then
        # later restored from a backup taken before that fix),
        # the restore brings back BOTH the up-to-date schema AND
        # the stale alembic_version. ``alembic upgrade head`` then
        # fails on the first migration with "table already exists".
        #
        # Detect that signature and recover by stamping head — the
        # schema is already correct, alembic_version just needs to
        # catch up.
        if any(p in msg for p in _DRIFT_ERROR_PATTERNS):
            stamp_ok, stamp_err = await _try_alembic_stamp_head(db_url)
            if stamp_ok:
                logger.info(
                    "backup_restore_alembic_drift_recovered",
                    source_head=source_head,
                    local_head=local_head,
                )
                return MigrationOutcome(
                    state="auto_recovered",
                    source_head=source_head,
                    local_head=local_head,
                    migrations_applied=[],
                    error=(
                        "alembic_version was stale but the restored schema is "
                        f"already at {local_head!r}; stamped head to align. "
                        "No migrations actually ran."
                    ),
                )
            return MigrationOutcome(
                state="failed",
                source_head=source_head,
                local_head=local_head,
                migrations_applied=[],
                error=(
                    f"alembic upgrade failed and stamp-head recovery also "
                    f"failed. Upgrade error: {msg}; stamp error: {stamp_err}"
                ),
            )

        return MigrationOutcome(
            state="failed",
            source_head=source_head,
            local_head=local_head,
            migrations_applied=[],
            error=f"alembic upgrade failed (exit {proc.returncode}): {msg}",
        )

    logger.info(
        "backup_restore_alembic_upgrade_applied",
        source_head=source_head,
        local_head=local_head,
        planned=planned,
    )
    return MigrationOutcome(
        state="upgraded",
        source_head=source_head,
        local_head=local_head,
        migrations_applied=planned,
    )


async def _try_alembic_stamp_head(db_url: str) -> tuple[bool, str | None]:
    """Run ``alembic stamp head`` against the configured database.
    Returns ``(success, error_message)``. Used to recover from
    schema-vs-alembic-version drift after a restore.
    """
    pg_env, _dbname = _pg_env_from_url(db_url)
    full_env = {**os.environ, **pg_env, "DATABASE_URL": db_url}
    cmd = ["alembic", "-c", str(_ALEMBIC_INI_PATH), "stamp", "head"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_ALEMBIC_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"alembic stamp timed out after {_ALEMBIC_TIMEOUT_SECONDS}s"
    if proc.returncode != 0:
        msg = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[:500]
        return False, f"alembic stamp head failed (exit {proc.returncode}): {msg}"
    return True, None
