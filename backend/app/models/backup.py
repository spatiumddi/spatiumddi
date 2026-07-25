"""SQLAlchemy models for the backup subsystem.

``backup_target`` (issue #117 Phase 1b) — one row per
operator-configured backup destination. Phase 1b shipped
``local_volume``; Phase 1c (S3) and 1d (SCP/Azure Blob) added new
``kind`` values without schema changes thanks to the JSONB
``config`` column.

``restore_drill`` (issue #702) — one row per restore-verification
drill: a scheduled test-restore of a target's newest archive into
a throwaway database, proving the archive is actually restorable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BackupTarget(Base):
    __tablename__ = "backup_target"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    passphrase_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    passphrase_hint: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    schedule_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)

    retention_keep_last_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_keep_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_run_status: Mapped[str] = mapped_column(String(20), nullable=False, default="never")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_run_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_run_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Restore-verification drills (issue #702). Separate cadence from
    # ``schedule_cron`` on purpose: a drill replays the *newest* archive
    # into a throwaway database, so it's worth running far less often
    # than the backup itself (weekly against nightly is the common
    # shape). ``drill_last_status`` is denormalised off ``restore_drill``
    # purely so the targets list can render a chip without a per-row
    # subquery — ``restore_drill`` stays the history of record.
    drill_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    drill_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)
    drill_next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drill_last_status: Mapped[str] = mapped_column(String(20), nullable=False, default="never")
    drill_last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RestoreDrill(Base):
    """One restore-verification drill run (issue #702).

    A drill downloads a target's newest archive, replays it into a
    throwaway scratch database, runs a fixed assertion set against
    the result, and drops the scratch database again. The live
    database is never written to — the whole point is to prove the
    archive is restorable *before* an operator needs it to be.

    ``assertions`` is the per-check verdict list
    (``[{"name": str, "status": "pass"|"fail"|"skip", "detail": str}]``)
    rather than a column per check, so adding a check later doesn't
    need a migration. ``state`` is the rollup: ``passed`` when no
    check failed — a ``skip`` (a check that couldn't be answered,
    e.g. schema skew) does not count against it.
    """

    __tablename__ = "restore_drill"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No ``index=True``: the composite ``ix_restore_drill_target_started``
    # in the migration leads with this column and serves single-column
    # lookups just as well. A second index here would only cost an extra
    # write per insert (and autogenerate would keep recreating it).
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("backup_target.id", ondelete="CASCADE"),
        nullable=False,
    )

    # "running" → terminal "passed" / "failed" / "error".
    # ``failed`` means the drill ran and an assertion did not hold —
    # that's a real finding about the archive. ``error`` means the
    # drill could not reach a verdict (destination unreachable,
    # scratch database couldn't be created); operationally distinct,
    # because only ``failed`` says anything about the backup itself.
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")

    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archive_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    scratch_db: Mapped[str | None] = mapped_column(String(80), nullable=True)

    assertions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
