"""Restore-verification drills (issue #702).

A drill answers the one question the backup subsystem could not:
*is the archive we wrote last night actually restorable?* It
downloads a target's newest archive, replays it into a throwaway
scratch database, runs a fixed assertion set against the result,
and drops the scratch database again.

**The live database is never written to.** Every subprocess and
every session in this module is pointed at a scratch database whose
name this module generates from a fixed prefix plus random hex —
never from operator input, and never equal to the live database
name (:func:`_scratch_db_name` asserts both).

Deliberately *not* built on :func:`app.services.backup.restore.apply_backup_restore`:
that function takes a pre-restore safety dump against the live
session and disposes the live engine's connection pool, which is
correct for an operator-initiated restore and actively wrong for an
unattended background drill. The replay helpers here are local for
the same reason — ``restore._run_psql`` terminates every other
connection to its target database, which must never become
something a drill can do.

Verdict vocabulary — ``failed`` and ``error`` are operationally
distinct and worth keeping apart:

* ``failed`` — the drill reached a verdict and the archive did not
  hold up (malformed, wrong passphrase, replay aborted, an
  assertion did not pass). This is a real finding about the backup.
* ``error`` — the drill could not reach a verdict at all
  (destination unreachable, scratch database couldn't be created).
  This says nothing about the archive.

Only ``failed`` should page someone about their backups.
"""

from __future__ import annotations

import asyncio
import os
import secrets as secrets_mod
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.core.crypto import decrypt_str
from app.models.backup import BackupTarget, RestoreDrill
from app.services.backup.archive import (
    BackupArchiveError,
    _pg_env_from_url,
    _pg_subprocess_env,
    extract_archive_members,
)
from app.services.backup.crypto import BackupCryptoError, decrypt_secrets
from app.services.backup.targets import (
    BackupDestinationError,
    SecretFieldError,
    decrypt_config_secrets,
    get_destination,
)

logger = structlog.get_logger(__name__)

SCRATCH_DB_PREFIX = "spatiumddi_drill_"

# A drill replays a full dump, so it gets the same envelope the
# restore path uses. The maintenance statements (CREATE / DROP
# DATABASE) are fast and get a much tighter bound.
_REPLAY_TIMEOUT_SECONDS = 30 * 60
_MAINTENANCE_TIMEOUT_SECONDS = 120

# How long a ``drill_last_status="in_progress"`` mutex is believed
# before the sweep treats it as stranded. A worker killed mid-drill
# never gets to stamp a terminal state, and without this the target
# would silently stop drilling forever. Comfortably above the replay
# timeout so a genuinely slow drill is never stolen from itself.
STALE_IN_PROGRESS_SECONDS = _REPLAY_TIMEOUT_SECONDS + 15 * 60

# Assertion verdicts.
PASS = "pass"
FAIL = "fail"
SKIP = "skip"


class DrillError(Exception):
    """Infrastructure failure — the drill could not reach a verdict.

    Distinct from a *failed* drill, which is a real finding about
    the archive. Raising this maps the run to ``state="error"``.
    """


@dataclass
class Assertion:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class DrillOutcome:
    state: str
    assertions: list[Assertion] = field(default_factory=list)
    filename: str | None = None
    archive_bytes: int | None = None
    manifest: dict[str, Any] | None = None
    scratch_db: str | None = None
    error: str | None = None


# ── scratch database lifecycle ────────────────────────────────────────


def _scratch_db_name(live_dbname: str) -> str:
    """Generate a throwaway database name.

    Fixed prefix + random hex, never operator-derived. The equality
    check against the live name is belt-and-braces — the prefix
    makes a collision essentially impossible — but this is the one
    place where a bug would replay a backup over production, so it
    is asserted explicitly rather than reasoned about.
    """
    name = f"{SCRATCH_DB_PREFIX}{secrets_mod.token_hex(6)}"
    if name == live_dbname:
        raise DrillError("generated scratch database name collided with the live database")
    return name


async def _run_maintenance_sql(sql: str, *, live_db_url: str, timeout: float) -> None:
    """Run a single statement against the ``postgres`` maintenance
    database. CREATE / DROP DATABASE cannot run inside a
    transaction block and cannot run while connected to the
    database being created or dropped, so these are issued against
    ``postgres`` rather than either the live or scratch database.
    """
    pg_env, _dbname = _pg_env_from_url(live_db_url)
    pg_env = {**pg_env, "PGDATABASE": "postgres"}
    proc = await asyncio.create_subprocess_exec(
        "psql",
        "--set=ON_ERROR_STOP=1",
        f"--command={sql}",
        env=_pg_subprocess_env(pg_env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise DrillError(f"maintenance statement exceeded {timeout}s timeout") from exc
    if proc.returncode != 0:
        msg = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[:800]
        raise DrillError(f"maintenance statement failed (exit {proc.returncode}): {msg}")


async def _create_scratch_db(name: str, *, live_db_url: str) -> None:
    await _run_maintenance_sql(
        f'CREATE DATABASE "{name}"',
        live_db_url=live_db_url,
        timeout=_MAINTENANCE_TIMEOUT_SECONDS,
    )


async def _drop_scratch_db(name: str, *, live_db_url: str) -> None:
    """Drop the scratch database, best-effort.

    Runs in a ``finally`` — a failure to clean up must not mask the
    drill's actual verdict, so this logs and returns rather than
    raising. ``WITH (FORCE)`` kicks any connection we left behind
    (Postgres 13+); the scratch database is ours alone, so there is
    nothing else that could legitimately be attached.
    """
    if not name.startswith(SCRATCH_DB_PREFIX):
        logger.error("restore_drill_refusing_drop", dbname=name)
        return
    try:
        await _run_maintenance_sql(
            f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
            live_db_url=live_db_url,
            timeout=_MAINTENANCE_TIMEOUT_SECONDS,
        )
    except DrillError as exc:
        # Leaves a stray database behind. Surfaced loudly because it
        # needs manual cleanup, but never fatal to the drill result.
        logger.error("restore_drill_scratch_drop_failed", dbname=name, error=str(exc))


def _scratch_url(live_db_url: str, scratch_db: str) -> str:
    """Swap the database name in a SQLAlchemy URL, preserving driver,
    credentials, and any query string (``?ssl=require`` and friends).
    """
    head, sep, query = live_db_url.partition("?")
    base, _, _old_dbname = head.rpartition("/")
    if not base:
        raise DrillError(f"could not derive a scratch URL from {live_db_url!r}")
    return f"{base}/{scratch_db}{sep}{query}"


# ── replay ────────────────────────────────────────────────────────────


async def _replay_into_scratch(
    db_bytes: bytes, *, dump_format: str, scratch_db: str, live_db_url: str
) -> None:
    """Replay the archive's dump member into the scratch database.

    Raises :class:`BackupArchiveError` on a non-zero exit — a dump
    that won't replay is a finding about the archive (``failed``),
    not an infrastructure error.
    """
    pg_env, _live = _pg_env_from_url(live_db_url)
    pg_env = {**pg_env, "PGDATABASE": scratch_db}
    env = _pg_subprocess_env(pg_env)

    with tempfile.TemporaryDirectory(prefix="spatium-drill-") as tmpdir:
        suffix = "sql" if dump_format == "plain" else "dump"
        dump_path = Path(tmpdir) / f"database.{suffix}"
        dump_path.write_bytes(db_bytes)
        os.chmod(dump_path, 0o600)

        if dump_format == "plain":
            cmd = [
                "psql",
                "--set=ON_ERROR_STOP=1",
                "--single-transaction",
                f"--file={dump_path}",
            ]
        else:
            cmd = [
                "pg_restore",
                "--dbname",
                scratch_db,
                "--no-owner",
                "--no-acl",
                "--single-transaction",
                "--exit-on-error",
                str(dump_path),
            ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_REPLAY_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise BackupArchiveError(f"replay exceeded {_REPLAY_TIMEOUT_SECONDS}s timeout") from exc
        if proc.returncode != 0:
            msg = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[:1500]
            raise BackupArchiveError(f"replay failed (exit {proc.returncode}): {msg}")


# ── assertions ────────────────────────────────────────────────────────

# Tables whose emptiness would make a restore useless. Deliberately
# short: a drill asserts the archive is *usable*, not that the
# operator's data looks a particular way.
_REQUIRED_NONEMPTY = ("user", "alembic_version")


async def _assert_against_scratch(scratch_url: str) -> list[Assertion]:
    """Open a session on the restored scratch database and run the
    post-replay checks."""
    out: list[Assertion] = []
    # NullPool: this engine lives for one drill and is disposed
    # immediately, and a pooled connection left open would block the
    # DROP DATABASE in the caller's ``finally``.
    engine = create_async_engine(scratch_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with maker() as session:
            head = await _assert_alembic_head(session, out)
            await _assert_required_tables(session, out)
            await _assert_audit_chain(session, out, restored_head=head)
    finally:
        await engine.dispose()
    return out


async def _assert_alembic_head(session: AsyncSession, out: list[Assertion]) -> str | None:
    """Assert the restored database declares exactly one alembic
    revision, and report how it relates to the bundled head.

    A restored head *behind* the bundled head is not a failure — the
    real restore path runs ``alembic upgrade head`` afterwards
    (``maybe_upgrade_after_restore``). A head the running build has
    never heard of is a failure: nothing can migrate it forward.
    """
    from app.core.schema_check import expected_alembic_head  # noqa: PLC0415

    try:
        rows = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars()
        versions = list(rows)
    except Exception as exc:  # noqa: BLE001 — any DB error is a finding
        out.append(
            Assertion(
                "alembic_version_present",
                FAIL,
                f"could not read alembic_version from the restored database: {exc}",
            )
        )
        return None

    if len(versions) != 1:
        out.append(
            Assertion(
                "alembic_version_present",
                FAIL,
                f"expected exactly 1 alembic_version row, found {len(versions)}"
                + (" (multi-head schema)" if len(versions) > 1 else ""),
            )
        )
        return None

    restored = versions[0]
    bundled, err = expected_alembic_head()
    if bundled is None:
        out.append(
            Assertion(
                "alembic_version_present",
                PASS,
                f"restored at {restored}; could not compare to the bundled head ({err})",
            )
        )
        return restored

    if restored == bundled:
        out.append(
            Assertion("alembic_version_present", PASS, f"restored at bundled head {restored}")
        )
        return restored

    if _is_known_revision(restored):
        out.append(
            Assertion(
                "alembic_version_present",
                PASS,
                f"restored at {restored}, behind bundled head {bundled} — "
                f"a real restore would migrate it forward",
            )
        )
        return restored

    out.append(
        Assertion(
            "alembic_version_present",
            FAIL,
            f"restored at {restored}, which this build does not know — "
            f"it cannot be migrated to the bundled head {bundled}. The "
            f"archive was taken by a newer SpatiumDDI than this one.",
        )
    )
    return restored


def _is_known_revision(revision: str) -> bool:
    """True when the bundled alembic scripts contain ``revision``."""
    try:
        from alembic.config import Config  # noqa: PLC0415
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        from app.core.schema_check import _locate_alembic_ini  # noqa: PLC0415

        ini = _locate_alembic_ini()
        if ini is None:
            return False
        script = ScriptDirectory.from_config(Config(str(ini)))
        return script.get_revision(revision) is not None
    except Exception:  # noqa: BLE001
        # Unknown revision id raises; so does a malformed script dir.
        # Either way we can't vouch for it.
        return False


async def _assert_required_tables(session: AsyncSession, out: list[Assertion]) -> None:
    """Assert the tables a restore is useless without came back with
    rows in them."""
    counts: dict[str, int | None] = {}
    for table in _REQUIRED_NONEMPTY:
        try:
            result = await session.execute(text(f'SELECT count(*) FROM "{table}"'))
            counts[table] = int(result.scalar_one())
        except Exception as exc:  # noqa: BLE001 — missing table is a finding
            out.append(
                Assertion(
                    "core_tables_populated",
                    FAIL,
                    f'could not count rows in "{table}": {exc}',
                )
            )
            return

    empty = [name for name, n in counts.items() if not n]
    summary = ", ".join(f"{name}={n}" for name, n in counts.items())
    if empty:
        out.append(
            Assertion(
                "core_tables_populated",
                FAIL,
                f"restored but empty: {', '.join(empty)} — an install restored "
                f"from this archive would have no way in ({summary})",
            )
        )
        return
    out.append(Assertion("core_tables_populated", PASS, summary))


async def _assert_audit_chain(
    session: AsyncSession, out: list[Assertion], *, restored_head: str | None
) -> None:
    """Verify the restored audit log's tamper-evident hash chain.

    Skipped when the restored schema isn't at the bundled head: the
    verifier selects through the ORM model, so a column added since
    the archive was taken would raise ``UndefinedColumn`` and read
    as tampering when it is really schema skew.
    """
    from app.core.schema_check import expected_alembic_head  # noqa: PLC0415
    from app.services.audit_chain import verify_chain  # noqa: PLC0415

    bundled, _err = expected_alembic_head()
    if bundled is not None and restored_head is not None and restored_head != bundled:
        out.append(
            Assertion(
                "audit_chain_intact",
                SKIP,
                f"restored schema is at {restored_head}, not the bundled head "
                f"{bundled} — the ORM-based verifier would misread schema skew "
                f"as tampering",
            )
        )
        return

    try:
        result = await verify_chain(session)
    except Exception as exc:  # noqa: BLE001
        out.append(
            Assertion("audit_chain_intact", SKIP, f"chain verification could not run: {exc}")
        )
        return

    if result.ok:
        out.append(Assertion("audit_chain_intact", PASS, f"{result.rows_checked} rows verified"))
        return
    first = result.breaks[0] if result.breaks else None
    detail = f"{len(result.breaks)} break(s) across {result.rows_checked} rows" + (
        f"; first at seq {first.seq} ({first.reason})" if first is not None else ""
    )
    out.append(Assertion("audit_chain_intact", FAIL, detail))


# ── orchestration ─────────────────────────────────────────────────────


async def run_drill_for_target(
    db: AsyncSession,
    *,
    target: BackupTarget,
    triggered_by: str = "manual",
) -> RestoreDrill:
    """Run one restore-verification drill against ``target``.

    Persists a ``running`` row up front so a long drill is visible
    in the UI while it works, then stamps the terminal state. The
    caller commits; the row is returned attached to ``db``.
    """
    started = datetime.now(UTC)
    row = RestoreDrill(target_id=target.id, state="running", triggered_by=triggered_by)
    db.add(row)
    target.drill_last_status = "in_progress"
    # While a drill is running this holds its *start* time, which is
    # what lets the sweep spot a mutex stranded by a killed worker
    # (see STALE_IN_PROGRESS_SECONDS). It's overwritten with the
    # finish time below.
    target.drill_last_at = started
    await db.commit()
    await db.refresh(row)

    live_db_url = str(settings.database_url)
    outcome = await _execute(target, live_db_url=live_db_url)

    finished = datetime.now(UTC)
    row.state = outcome.state
    row.assertions = [a.as_dict() for a in outcome.assertions]
    row.filename = outcome.filename
    row.archive_bytes = outcome.archive_bytes
    row.manifest = outcome.manifest
    row.scratch_db = outcome.scratch_db
    row.error = outcome.error
    row.finished_at = finished
    row.duration_ms = int((finished - started).total_seconds() * 1000)

    target.drill_last_status = outcome.state
    target.drill_last_at = finished
    if target.drill_enabled and target.drill_cron:
        from app.services.backup.schedule import compute_next_run  # noqa: PLC0415

        try:
            target.drill_next_run_at = compute_next_run(target.drill_cron, after=finished)
        except Exception as exc:  # noqa: BLE001
            # A bad cron shouldn't wedge the sweep on this row forever.
            logger.warning(
                "restore_drill_next_run_failed",
                target_id=str(target.id),
                cron=target.drill_cron,
                error=str(exc),
            )
            target.drill_next_run_at = None

    logger.info(
        "restore_drill_finished",
        target_id=str(target.id),
        state=outcome.state,
        filename=outcome.filename,
        duration_ms=row.duration_ms,
    )
    return row


async def _execute(target: BackupTarget, *, live_db_url: str) -> DrillOutcome:
    """Do the work, translating every failure mode into a verdict.

    Never raises: the caller always gets a ``DrillOutcome`` to
    persist.
    """
    assertions: list[Assertion] = []

    # 1. Fetch the newest archive from the destination.
    try:
        driver = get_destination(target.kind)
        plain_config = decrypt_config_secrets(driver, target.config)
        listings = await driver.list_archives(config=plain_config)
    except (BackupDestinationError, SecretFieldError, ValueError) as exc:
        return DrillOutcome(state="error", error=f"could not reach the destination: {exc}")

    if not listings:
        assertions.append(
            Assertion(
                "archive_available",
                FAIL,
                "the destination holds no archives — there is nothing to restore from",
            )
        )
        return DrillOutcome(state="failed", assertions=assertions)

    newest = max(listings, key=lambda a: a.created_at)
    assertions.append(
        Assertion("archive_available", PASS, f"{newest.filename} ({newest.size_bytes} bytes)")
    )

    try:
        archive_bytes = await driver.download(config=plain_config, filename=newest.filename)
    except BackupDestinationError as exc:
        return DrillOutcome(
            state="error",
            filename=newest.filename,
            assertions=assertions,
            error=f"download failed: {exc}",
        )
    if not archive_bytes:
        assertions.append(
            Assertion("archive_readable", FAIL, "archive downloaded empty from the destination")
        )
        return DrillOutcome(state="failed", filename=newest.filename, assertions=assertions)

    base = DrillOutcome(
        state="failed",
        filename=newest.filename,
        archive_bytes=len(archive_bytes),
        assertions=assertions,
    )

    # 2. Parse it, and prove the stored passphrase opens it.
    try:
        manifest, db_bytes, dump_format, secrets_enc = extract_archive_members(archive_bytes)
    except BackupArchiveError as exc:
        assertions.append(Assertion("archive_readable", FAIL, str(exc)))
        return base
    base.manifest = manifest
    assertions.append(
        Assertion(
            "archive_readable",
            PASS,
            f"format_version={manifest.get('format_version')} dump_format={dump_format}",
        )
    )

    try:
        passphrase = decrypt_str(target.passphrase_encrypted)
    except Exception as exc:  # noqa: BLE001
        # The stored passphrase itself is unreadable — that's this
        # install's key management, not the archive.
        return DrillOutcome(
            state="error",
            filename=newest.filename,
            archive_bytes=len(archive_bytes),
            manifest=manifest,
            assertions=assertions,
            error=f"could not decrypt the target's stored passphrase: {exc}",
        )

    try:
        payload = decrypt_secrets(secrets_enc, passphrase=passphrase)
    except BackupCryptoError as exc:
        assertions.append(
            Assertion(
                "passphrase_opens_secrets",
                FAIL,
                f"the passphrase stored on this target does not open the archive's "
                f"secrets.enc — a restore from it would be impossible ({exc})",
            )
        )
        return base
    assertions.append(
        Assertion(
            "passphrase_opens_secrets",
            PASS,
            f"{len(payload)} secret key(s) recovered",
        )
    )

    # 3. Replay into a throwaway database and assert against it.
    try:
        _pg_env, live_dbname = _pg_env_from_url(live_db_url)
        scratch_db = _scratch_db_name(live_dbname)
    except (BackupArchiveError, DrillError) as exc:
        return DrillOutcome(
            state="error",
            filename=newest.filename,
            archive_bytes=len(archive_bytes),
            manifest=manifest,
            assertions=assertions,
            error=str(exc),
        )
    base.scratch_db = scratch_db

    try:
        await _create_scratch_db(scratch_db, live_db_url=live_db_url)
    except DrillError as exc:
        return DrillOutcome(
            state="error",
            filename=newest.filename,
            archive_bytes=len(archive_bytes),
            manifest=manifest,
            scratch_db=scratch_db,
            assertions=assertions,
            error=f"could not create the scratch database: {exc}",
        )

    try:
        try:
            await _replay_into_scratch(
                db_bytes,
                dump_format=dump_format,
                scratch_db=scratch_db,
                live_db_url=live_db_url,
            )
        except BackupArchiveError as exc:
            assertions.append(Assertion("dump_replays_cleanly", FAIL, str(exc)))
            return base
        assertions.append(Assertion("dump_replays_cleanly", PASS, f"replayed into {scratch_db}"))

        try:
            assertions.extend(await _assert_against_scratch(_scratch_url(live_db_url, scratch_db)))
        except Exception as exc:  # noqa: BLE001
            return DrillOutcome(
                state="error",
                filename=newest.filename,
                archive_bytes=len(archive_bytes),
                manifest=manifest,
                scratch_db=scratch_db,
                assertions=assertions,
                error=f"post-replay assertions could not run: {exc}",
            )
    finally:
        await _drop_scratch_db(scratch_db, live_db_url=live_db_url)

    base.state = "failed" if any(a.status == FAIL for a in assertions) else "passed"
    return base
