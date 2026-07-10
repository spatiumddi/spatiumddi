"""SQLAlchemy models for Scheduled Wake-on-LAN (issue #586).

Phase 1 tables (``wol_schedule`` / ``wol_run`` / ``wol_run_target``) plus the
Phase 2 external-calendar gate (``wol_calendar`` + ``wol_calendar_event`` and
the three ``wol_schedule.calendar_*`` columns).

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

The built-in holiday gate is ``blackout_dates`` + ``active_from`` /
``active_until`` + ``timezone``; Phase 2 layers the external
``wol_calendar`` / ``wol_calendar_event`` gate on top via the three
``wol_schedule.calendar_*`` columns.

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
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# ``wol_run.verify_state`` lifecycle values. Terminal is ``done``. Declared on the
# model (not the Celery task that drives the machine) so readers outside the task
# — the alert evaluator, the MCP tools — can key on them without importing the
# Celery bootstrap. ``app.tasks.wol_scheduler`` re-exports these under its own
# VERIFY_* names.
VERIFY_STATE_NONE = "none"  # verify off / never scheduled
VERIFY_STATE_PENDING = "pending"  # a pass is enqueued, awaiting its atomic claim
VERIFY_STATE_VERIFYING = "verifying"  # a pass holds the run's verify mutex
VERIFY_STATE_DONE = "done"  # finalised (all up, or retries exhausted)

# ``wol_run.status`` values. On the model (not just the Celery task) so the
# ad-hoc IPAM wake can stamp a run without importing the Celery bootstrap.
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_IN_PROGRESS = "in_progress"


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

    # ── External calendar gate (Phase 2 — issue #586) ───────────────────
    # Optional subscription (iCal .ics URL or authenticated CalDAV) whose
    # all-day event spans ADD a gate on top of the built-in blackout/term
    # checks above. ``calendar_mode`` decides the polarity:
    #   * ``none``          — calendar ignored (default; pure Phase-1 behaviour).
    #   * ``skip_on_event`` — a matching event on the local fire date SKIPS
    #                         the wake (holiday calendar).
    #   * ``only_on_event`` — the wake only fires when a matching event covers
    #                         the local fire date (term / school-day calendar).
    # ``calendar_match`` (optional) is a regex ANDed onto which events count
    # (matched against the event summary + each category, case-insensitive).
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wol_calendar.id", ondelete="SET NULL"),
        nullable=True,
    )
    calendar_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    calendar_match: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # ── Post-wake liveness verify + retry (Phase 3 — issue #586) ─────────
    # After a run dispatches wakes, an optional chained Celery task
    # (app.tasks.wol_scheduler.verify_wol_run) probes each SENT host for
    # liveness and re-wakes non-responders up to a bound. v1 probes from the
    # control-plane SERVER vantage (ping) regardless of the wake vantage — see
    # verify.py for the appliance-vantage-verify deferral rationale.
    verify_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Grace between dispatch (and between retry passes) and the liveness probe.
    verify_wait_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    # Number of *re-wake* passes after the first probe (total probe passes ≤
    # verify_retries + 1). 0 == probe once, never re-wake.
    verify_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # 'ping'-only in v1 (kept as a column so a future TCP/agent method needs
    # no migration).
    # Per-schedule mute for the ``wol_wake_failed`` alert (#596 Phase 2). The
    # rule's own ``enabled`` flag is the master switch; this silences one noisy
    # schedule without turning the rule off fleet-wide.
    verify_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    verify_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ping", server_default=text("'ping'")
    )

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
    calendar: Mapped[WolCalendar | None] = relationship(
        "WolCalendar",
        back_populates="schedules",
        foreign_keys=[calendar_id],
    )


class WolCalendar(Base):
    """A subscribed iCal / CalDAV calendar whose all-day event spans gate
    scheduled wakes (Phase 2 — issue #586).

    ``kind`` is ``ical_url`` (unauthenticated / token-in-URL ``.ics`` or
    ``webcal://`` feed — covers Google Calendar public links + most published
    school calendars) or ``caldav`` (authenticated collection — Nextcloud /
    Radicale / school servers). The CalDAV password is Fernet-encrypted at
    rest and never returned by the API (only a ``password_set`` boolean).

    A background reconciler (:mod:`app.tasks.wol_calendar`) pulls the feed on
    the ``refresh_interval_minutes`` cadence and flattens its all-day VEVENTs
    (recurrence expanded over a bounded forward horizon) into
    :class:`WolCalendarEvent` date spans for O(events) gate checks + a UI
    preview — the same last-known-good-cache shape the DNS blocklist feed uses.
    """

    __tablename__ = "wol_calendar"
    __table_args__ = (Index("ix_wol_calendar_name", "name"),)

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
    # ical_url | caldav
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Fernet ciphertext (LargeBinary). NULL == no password. Never serialised
    # back out — the Read schema exposes only ``password_set``.
    password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    refresh_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=360, server_default="360"
    )

    # ── Sync state (mirrors DNSBlockList / UnifiController) ──────────────
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # success | error
    last_sync_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # denormalised count of currently-cached event spans (UI list column).
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    events: Mapped[list[WolCalendarEvent]] = relationship(
        "WolCalendarEvent",
        back_populates="calendar",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    schedules: Mapped[list[WolSchedule]] = relationship(
        "WolSchedule",
        back_populates="calendar",
        foreign_keys="WolSchedule.calendar_id",
    )


class WolCalendarEvent(Base):
    """A flattened all-day event span pulled from a :class:`WolCalendar`.

    Recurrence (RRULE / RDATE) is expanded at sync time over a bounded forward
    horizon, so each row is a single concrete ``[starts_on, ends_on]`` inclusive
    date span (``ends_on`` is already the RFC 5545 exclusive-DTEND minus a day).
    The gate compares the schedule-local fire date against these spans directly
    — no tz shift, all-day dates are floating.
    """

    __tablename__ = "wol_calendar_event"
    __table_args__ = (
        # UNIQUE on the reconcile natural key (calendar-scoped span + uid) is the
        # backstop against a concurrent-sync duplicate: the inline sync-now (API
        # process) and the beat sweep (worker process) can otherwise both read an
        # empty ``existing`` for a fresh span and each insert it. The per-calendar
        # row lock in ``sync_calendar`` serialises them; this constraint makes the
        # duplicate physically impossible even if the lock is ever bypassed.
        # ``NULLS NOT DISTINCT`` (PG15+) is required because ``uid`` is nullable
        # and PG otherwise treats every NULL uid as distinct, which would let
        # NULL-uid spans duplicate anyway. The leading
        # (calendar_id, starts_on, ends_on) columns also serve the gate load +
        # upcoming-events span queries, so this replaces the old plain span index.
        Index(
            "uq_wol_calendar_event_natural",
            "calendar_id",
            "starts_on",
            "ends_on",
            "uid",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    calendar_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wol_calendar.id", ondelete="CASCADE"),
        nullable=False,
    )
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    # inclusive end (DTEND is exclusive for all-day VEVENTs → stored -1 day).
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    categories: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # VEVENT UID (recurrence occurrences share it) — natural key for the
    # set-reconcile diff alongside the span.
    uid: Mapped[str | None] = mapped_column(String(255), nullable=True)

    calendar: Mapped[WolCalendar] = relationship("WolCalendar", back_populates="events")


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
    # schedule | manual | adhoc  (adhoc == a single-host wake from the IPAM
    # address action that opted into post-wake verify; schedule_id is NULL)
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

    # ── Post-wake verify rollup (Phase 3 — issue #586) ──────────────────
    # verify_state lifecycle: none (verify off / never scheduled) → pending
    # (verify enqueued, awaiting the claim) → verifying (a probe pass holds the
    # mutex) → done (finalised, terminal). verified_count / unverified_count are
    # the SENT-target liveness rollup written at finalise.
    verify_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default=text("'none'")
    )
    verified_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unverified_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Verify mutex lease + attempt anchor (crash-recovery for the verify state
    # machine — the schedule mutex's ``in_progress_since`` equivalent).
    # ``verify_claimed_at`` is stamped on every transition INTO ``pending`` (the
    # arm / re-wake reset / reaper reset) AND into ``verifying`` (the claim); the
    # sweep's verify reaper reclaims rows whose lease is older than
    # ``VERIFY_CLAIM_LEASE_SECONDS``. ``verify_attempt`` is the run-level attempt
    # anchor — the claim keys on it so a stale ``acks_late`` redelivery of
    # attempt N no-ops once a re-wake has advanced the row to N+1.
    verify_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verify_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    # Per-run verify config snapshot (#596 Phase 1b). NULL for scheduled runs —
    # they read their live ``wol_schedule`` row, so an operator edit mid-flight
    # still takes effect. Ad-hoc runs (``schedule_id IS NULL``) have no parent to
    # read, so they carry their own config here and it is the sole source of
    # truth: {"method", "wait_seconds", "retries", "vantage", "port",
    # "repeat_count", "repeat_interval_ms"} — every key optional, per-key
    # fallback to the model defaults.
    verify_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

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

    # ── Post-wake verify outcome (Phase 3 — issue #586) ─────────────────
    # verified tri-state: NULL == not-yet/not-checked (verify off, or a
    # skipped/failed/address-less target never probed) · False == probed and
    # DOWN (a re-wake candidate) · True == probed and UP.
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Which source SETTLED the verdict — ping | tcp | seen (never the ``auto``
    # keyword). NULL when no source could run against this row.
    verify_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Ordered trail of every source consulted on the final pass (#596 Phase 3):
    # [{source, up, detail, observed_at}]. Answers "down according to what?".
    # NULL on rows written before the trail shipped, and on rows no source ran
    # against.
    verify_evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # 1 == original dispatch; each re-wake pass bumps it.
    wake_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    run: Mapped[WolRun] = relationship("WolRun", back_populates="targets")
