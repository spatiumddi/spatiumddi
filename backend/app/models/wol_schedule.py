"""SQLAlchemy models for Scheduled Wake-on-LAN — Phase 1 (issue #586).

Three tables:

* ``wol_schedule`` — the recurring, tag-targeted wake job. Carries a
  cron expression (NULL == manual-only, never swept), an IANA timezone
  so the cron fires on the operator's wall clock across DST, a JSONB
  ``target_selector`` (see the resolver in
  ``app.services.wol_scheduler.targets``), the send knobs the #533 send
  path honours (``vantage`` / ``repeat_count`` / ``stagger_ms`` / ``port``),
  and denormalised ``next_run_at`` (UTC) which is the beat sweep's
  due-query key.
* ``wol_run`` — one execution-history row per fire (scheduled OR manual),
  including gated-skip runs so "skipped because holiday" is visible.
* ``wol_run_target`` — per-host outcome (sent / skipped / failed) child
  of ``wol_run``.

**Phase 1 only** — the built-in holiday gate is ``blackout_dates`` +
``active_from`` / ``active_until`` + ``timezone``. There is NO external
iCal / CalDAV calendar FK; that is Phase 2 and deliberately absent here.

Reuses the shipped #533 send path (``app.services.wol``) — this module
adds scheduling state only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class WolSchedule(Base):
    """A recurring (or manual-only) Wake-on-LAN job."""

    __tablename__ = "wol_schedule"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # {mode, tags[], subnet_ids[], address_ids[]} — resolved by
    # app.services.wol_scheduler.targets.resolve_wol_targets.
    target_selector: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # NULL cron == manual-only (never swept by the beat task).
    schedule_cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # IANA tz — cron walks wall-clock in this zone (DST-safe).
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )

    # ── Built-in holiday gate (Phase 1 — no external calendar) ──────────
    # list of ISO "YYYY-MM-DD" strings.
    blackout_dates: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    active_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    active_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    # ── Send options (honoured by the #533 send path) ───────────────────
    # NetToolTarget shape {kind, id}; default = control-plane server vantage.
    vantage: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"kind": "server", "id": None},
        server_default=func.jsonb_build_object("kind", "server", "id", None),
    )
    repeat_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    repeat_interval_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    stagger_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=9, server_default="9")

    # ── Last-run mirror (denormalised for the list view) ────────────────
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ok | partial | skipped | failed | in_progress
    last_run_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_run_skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_target_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # UTC instant the current in_progress claim was stamped — the mutex lease.
    # Set atomically when a runner claims the row, cleared on every terminal
    # path. A crashed worker leaves it set; the sweep reclaims rows whose lease
    # is older than ``CLAIM_LEASE_SECONDS`` instead of skipping them forever.
    in_progress_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # denormalised UTC — the beat sweep's due-query key.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    runs: Mapped[list[WolRun]] = relationship(
        "WolRun",
        back_populates="schedule",
        passive_deletes=True,
    )


class WolRun(Base):
    """One execution-history row per fire (scheduled or manual)."""

    __tablename__ = "wol_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # SET NULL so history survives a schedule delete ("skipped because
    # holiday" stays visible).
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wol_schedule.id", ondelete="SET NULL"),
        nullable=True,
    )
    # schedule | manual
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ok | partial | skipped | failed | in_progress
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # run-level gate skip enum — NULL unless status == "skipped".
    skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    target_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # NULL for beat-fired system runs.
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    schedule: Mapped[WolSchedule | None] = relationship("WolSchedule", back_populates="runs")
    targets: Mapped[list[WolRunTarget]] = relationship(
        "WolRunTarget",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WolRunTarget(Base):
    """Per-host outcome for a single wake attempt within a run."""

    __tablename__ = "wol_run_target"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wol_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_address.id", ondelete="SET NULL"),
        nullable=True,
    )
    # IP string snapshot (survives IP-row delete).
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # canonical aa:bb:cc:dd:ee:ff (NULL when skipped no_mac).
    mac: Mapped[str | None] = mapped_column(String(17), nullable=True)
    # segment key (dedupe + appliance vantage NIC pick) — plain col, no FK.
    subnet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    broadcast: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # {kind, id} the packet was sent from.
    vantage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # ip | history | lease — which fallback step resolved the MAC.
    mac_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # per-host skip enum — NULL when sent == true.
    skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[WolRun] = relationship("WolRun", back_populates="targets")
