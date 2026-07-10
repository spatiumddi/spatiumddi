"""Pydantic schemas for the Scheduled Wake-on-LAN REST API — Phase 1
(issue #586).

Mounted at ``/api/v1/wake-scheduler`` (module ``tools.wake_scheduler``).
The wire contract is the ``wake-scheduler`` prefix even though the Python
package is ``wol_schedules`` — the frontend api client, the MCP tools, and
the runner docstrings all reference ``/wake-scheduler``.

The built-in holiday gate is ``blackout_dates`` + ``active_from`` /
``active_until`` + ``timezone``; Phase 2 (issue #586) layers an external
iCal / CalDAV calendar gate on top (``calendar_id`` / ``calendar_mode`` /
``calendar_match`` on the schedule + the ``WakeCalendar*`` shapes below).

Validation reuses the shipped service helpers verbatim so the API and the
beat runner agree on what a valid schedule is:

* ``validate_cron`` / ``validate_timezone`` (``app.services.wol_scheduler.schedule``)
* ``VALID_MODES`` (``app.services.wol_scheduler.resolver``)
* ``NetToolTarget`` (``app.services.nettools.schemas``) for the vantage.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.nettools.schemas import NetToolTarget
from app.services.wol_scheduler.schedule import (
    InvalidCronExpression,
    InvalidTimezone,
    validate_cron,
    validate_timezone,
)

# Selector modes (mirrors ``app.services.wol_scheduler.resolver.VALID_MODES``).
SelectorMode = Literal["address_tags", "subnet", "subnet_tags", "hosts"]

# Calendar gate polarities (mirror ``wol_schedule.calendar_mode`` +
# ``app.services.wol_scheduler.gating`` CAL_MODE_*). Phase 2 (issue #586).
CalendarMode = Literal["none", "skip_on_event", "only_on_event"]

# Calendar subscription kinds (mirror ``wol_calendar.kind``).
CalendarKind = Literal["ical_url", "caldav"]

# Vantage kinds Phase-1 WoL can actually send from (a magic packet only
# originates from the control-plane server or a Fleet appliance NIC).
_ALLOWED_VANTAGE_KINDS = frozenset({"server", "appliance"})


# ── Sub-shapes ───────────────────────────────────────────────────────


class TargetSelectorIn(BaseModel):
    """The stored ``target_selector`` JSONB, validated on the way in.

    ``tags`` are the ``?tag=key`` / ``?tag=key:value`` grammar (ANDed) for
    the tag modes; ``subnet_ids`` / ``address_ids`` are the explicit-id
    modes. Only the list relevant to ``mode`` is consulted by the resolver,
    but all four fields are carried so the operator can flip modes without
    losing the other selections.
    """

    mode: SelectorMode
    tags: list[str] = Field(default_factory=list)
    subnet_ids: list[uuid.UUID] = Field(default_factory=list)
    address_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_tags_for_tag_modes(self) -> TargetSelectorIn:
        """Reject an empty / whitespace-only ``tags`` list for the tag modes.

        Without this a ``{mode:'address_tags', tags:[]}`` selector would
        resolve to *every* unicast host in scope (``apply_tag_filter`` returns
        the statement unchanged for an empty tag list) — a platform-wide mass
        wake. The resolver has a matching defensive match-nothing guard for any
        row already stored before this validator existed.
        """
        if self.mode in ("address_tags", "subnet_tags"):
            cleaned = [t for t in self.tags if t and t.strip()]
            if not cleaned:
                raise ValueError(f"target_selector.tags must be non-empty for mode {self.mode!r}")
        return self

    def to_jsonb(self) -> dict[str, Any]:
        """Serialise to the resolver's stored shape (UUIDs → str)."""
        return {
            "mode": self.mode,
            "tags": list(self.tags),
            "subnet_ids": [str(s) for s in self.subnet_ids],
            "address_ids": [str(a) for a in self.address_ids],
        }


def _validate_vantage(v: NetToolTarget | None) -> NetToolTarget | None:
    """Reject a vantage kind WoL can't send from (Phase 1: server/appliance)."""
    if v is None:
        return v
    if v.kind not in _ALLOWED_VANTAGE_KINDS:
        raise ValueError(
            f"vantage.kind must be one of {sorted(_ALLOWED_VANTAGE_KINDS)}, got {v.kind!r}"
        )
    if v.kind == "appliance" and v.id is None:
        raise ValueError("vantage.id is required when vantage.kind is 'appliance'")
    return v


def _validate_calendar_match(value: str | None) -> str | None:
    """Ensure ``calendar_match`` is a compilable regex (or empty/None)."""
    if value is None or not value.strip():
        return value
    import re  # noqa: PLC0415

    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"calendar_match is not a valid regular expression: {exc}") from exc
    return value


def _validate_blackouts(value: list[str] | None) -> list[str] | None:
    """Ensure every blackout entry is an ISO ``YYYY-MM-DD`` string."""
    if value is None:
        return value
    out: list[str] = []
    for raw in value:
        try:
            out.append(date.fromisoformat(str(raw).strip()).isoformat())
        except ValueError as exc:
            raise ValueError(f"blackout date {raw!r} is not an ISO YYYY-MM-DD date") from exc
    return out


# ── Create / Update ──────────────────────────────────────────────────


class WakeScheduleCreate(BaseModel):
    """Operator request to create a Wake-on-LAN schedule."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    enabled: bool = True

    target_selector: TargetSelectorIn

    # NULL / empty cron == manual-only (never swept by the beat task).
    schedule_cron: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="UTC", max_length=64)

    # Built-in holiday gate (Phase 1 — no external calendar).
    blackout_dates: list[str] | None = None
    active_from: date | None = None
    active_until: date | None = None

    # External calendar gate (Phase 2). ``calendar_mode`` != 'none' requires
    # ``calendar_id``. ``calendar_match`` is an optional summary/category regex.
    calendar_id: uuid.UUID | None = None
    calendar_mode: CalendarMode = "none"
    calendar_match: str | None = None

    # Send options honoured by the #533 send path.
    vantage: NetToolTarget | None = None
    repeat_count: int = Field(default=2, ge=1, le=10)
    repeat_interval_ms: int = Field(default=100, ge=0, le=10_000)
    # ``stagger_ms == 0`` means "auto" — the runner ramps a large fleet via
    # ``auto_stagger_ms`` so a same-second all-at-once fire can't power-inrush /
    # PXE-thundering-herd. Any positive value is an explicit operator override
    # that always wins verbatim. See the ``suggested_stagger_ms`` on preview.
    stagger_ms: int = Field(default=0, ge=0, le=60_000)
    port: int = Field(default=9, ge=1, le=65535)

    # Post-wake liveness verify + bounded retry (Phase 3 — issue #586).
    verify_enabled: bool = False
    verify_wait_seconds: int = Field(default=60, ge=5, le=3600)
    # Number of *re-wake* passes after the first probe (0 == probe once, never
    # re-wake); total probe passes ≤ verify_retries + 1.
    verify_retries: int = Field(default=1, ge=0, le=10)
    # Liveness source (issue #596). ``auto`` walks ping → tcp → seen and stops at
    # the first confirmation, so it costs one ping against a live host and only
    # pays for the extra sources on hosts a ping-only verify would have re-woken
    # for nothing. Existing schedules keep whatever they stored; only new ones
    # default to ``auto``.
    verify_method: Literal["ping", "tcp", "seen", "auto"] = "auto"

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            validate_timezone(v)
        except InvalidTimezone as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("schedule_cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            validate_cron(v)
        except InvalidCronExpression as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("blackout_dates")
    @classmethod
    def _check_blackouts(cls, v: list[str] | None) -> list[str] | None:
        return _validate_blackouts(v)

    @field_validator("vantage")
    @classmethod
    def _check_vantage(cls, v: NetToolTarget | None) -> NetToolTarget | None:
        return _validate_vantage(v)

    @field_validator("calendar_match")
    @classmethod
    def _check_cal_match(cls, v: str | None) -> str | None:
        return _validate_calendar_match(v)

    @model_validator(mode="after")
    def _check_term_range(self) -> WakeScheduleCreate:
        if (
            self.active_from is not None
            and self.active_until is not None
            and self.active_from > self.active_until
        ):
            raise ValueError("active_from must be on or before active_until")
        if self.calendar_mode != "none" and self.calendar_id is None:
            raise ValueError(
                f"calendar_id is required when calendar_mode is {self.calendar_mode!r}"
            )
        return self


class WakeScheduleUpdate(BaseModel):
    """PATCH body — every field optional. ``model_dump(exclude_unset=True)``
    distinguishes "leave unchanged" from an explicit ``null`` on the nullable
    columns (``description`` / ``schedule_cron`` / ``blackout_dates`` /
    ``active_from`` / ``active_until``)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    enabled: bool | None = None

    target_selector: TargetSelectorIn | None = None

    schedule_cron: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)

    blackout_dates: list[str] | None = None
    active_from: date | None = None
    active_until: date | None = None

    # Calendar gate (Phase 2). ``calendar_id`` nullable-clearable; setting
    # ``calendar_mode`` to a non-'none' value with no attached calendar is
    # rejected in the router (needs the row to check the current calendar_id).
    calendar_id: uuid.UUID | None = None
    calendar_mode: CalendarMode | None = None
    calendar_match: str | None = None

    vantage: NetToolTarget | None = None
    repeat_count: int | None = Field(default=None, ge=1, le=10)
    repeat_interval_ms: int | None = Field(default=None, ge=0, le=10_000)
    stagger_ms: int | None = Field(default=None, ge=0, le=60_000)
    port: int | None = Field(default=None, ge=1, le=65535)

    # Verify config (Phase 3). All optional — omit == leave unchanged.
    verify_enabled: bool | None = None
    verify_wait_seconds: int | None = Field(default=None, ge=5, le=3600)
    verify_retries: int | None = Field(default=None, ge=0, le=10)
    verify_method: Literal["ping", "tcp", "seen", "auto"] | None = None

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            validate_timezone(v)
        except InvalidTimezone as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("schedule_cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return v
        try:
            validate_cron(v)
        except InvalidCronExpression as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("blackout_dates")
    @classmethod
    def _check_blackouts(cls, v: list[str] | None) -> list[str] | None:
        return _validate_blackouts(v)

    @field_validator("vantage")
    @classmethod
    def _check_vantage(cls, v: NetToolTarget | None) -> NetToolTarget | None:
        return _validate_vantage(v)

    @field_validator("calendar_match")
    @classmethod
    def _check_cal_match(cls, v: str | None) -> str | None:
        return _validate_calendar_match(v)


# ── Read ─────────────────────────────────────────────────────────────


class WakeScheduleRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    enabled: bool
    target_selector: dict[str, Any]
    schedule_cron: str | None
    timezone: str
    blackout_dates: list[str] | None
    active_from: date | None
    active_until: date | None
    calendar_id: uuid.UUID | None
    calendar_mode: str
    calendar_match: str | None
    vantage: dict[str, Any]
    repeat_count: int
    repeat_interval_ms: int
    stagger_ms: int
    port: int
    verify_enabled: bool
    verify_wait_seconds: int
    verify_retries: int
    verify_method: str
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_skip_reason: str | None
    last_target_count: int | None
    next_run_at: datetime | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    modified_at: datetime


class WakeTargetRead(BaseModel):
    """A host that WOULD be sent a magic packet (preview) — resolver
    ``WakeTarget`` flattened for the wire."""

    ip_address_id: uuid.UUID | None
    address: str | None
    mac: str
    subnet_id: uuid.UUID | None
    broadcast: str
    mac_source: str
    hostname: str | None = None


class SkippedTargetRead(BaseModel):
    """A matched input that would NOT be sent, with a reason (preview)."""

    reason: str
    ip_address_id: uuid.UUID | None = None
    address: str | None = None
    subnet_id: uuid.UUID | None = None


class TargetPreviewRequest(BaseModel):
    """Body for the unsaved ``POST /preview-targets`` — resolves a selector
    the operator is still editing (the create modal's live match count)."""

    target_selector: TargetSelectorIn


class TargetPreviewRead(BaseModel):
    """Resolver output for a preview: bucket counts + a capped sample of each
    bucket + (for a saved schedule) the next fire time and the built-in gate
    verdict at that fire."""

    matched_count: int
    wake_count: int
    skipped_count: int
    # Per-host skips whose reason is ``no_mac`` — the most common miss, worth
    # surfacing on its own so the operator sees "N hosts have no known MAC".
    mac_less_count: int
    sample: list[WakeTargetRead]
    skipped_sample: list[SkippedTargetRead]
    # Stagger auto-tune (Phase 3): the suggested inter-host gap (ms) for the
    # resolved ``wake_count`` when ``stagger_ms`` is left at 0/auto. The modal
    # surfaces this so the operator sees "waking N hosts → suggest ~X ms".
    suggested_stagger_ms: int = 0
    # Only populated for a saved-schedule preview (unsaved has no cron/gate).
    next_run_at: datetime | None = None
    gate_verdict: str | None = None


class WakeRunRead(BaseModel):
    id: uuid.UUID
    schedule_id: uuid.UUID | None
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    skip_reason: str | None
    target_count: int
    sent_count: int
    skipped_count: int
    failed_count: int
    # Post-wake verify rollup (Phase 3). ``verify_state``:
    # none|pending|verifying|done. ``verified_count`` / ``unverified_count`` are
    # the SENT-target liveness split, populated at verify finalise.
    verify_state: str
    verified_count: int
    unverified_count: int
    triggered_by_user_id: uuid.UUID | None
    error: str | None
    created_at: datetime


class WakeRunTargetRead(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    ip_address_id: uuid.UUID | None
    address: str | None
    mac: str | None
    subnet_id: uuid.UUID | None
    broadcast: str | None
    vantage: dict[str, Any] | None
    mac_source: str | None
    sent: bool
    skip_reason: str | None
    error: str | None
    # Post-wake verify outcome (Phase 3). ``verified`` tri-state: NULL ==
    # not-yet/not-checked · False == probed DOWN · True == probed UP.
    # ``wake_attempts`` is 1 for the original dispatch, +1 per re-wake pass.
    verified: bool | None
    verified_at: datetime | None
    verify_method: str | None
    wake_attempts: int
    created_at: datetime


class WakeRunDetailRead(WakeRunRead):
    """A run plus its per-host ``wol_run_target`` outcomes."""

    targets: list[WakeRunTargetRead]


# ── Calendars (Phase 2) ──────────────────────────────────────────────


class WakeCalendarCreate(BaseModel):
    """Operator request to subscribe a calendar (iCal .ics URL or CalDAV)."""

    name: str = Field(min_length=1, max_length=255)
    kind: CalendarKind
    url: str = Field(min_length=1)
    username: str | None = Field(default=None, max_length=255)
    # Write-only; encrypted at rest, never returned. A CalDAV subscription
    # usually needs one; an ical_url token feed usually doesn't.
    password: str | None = None
    enabled: bool = True
    refresh_interval_minutes: int = Field(default=360, ge=5, le=10_080)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        u = v.strip()
        lowered = u.lower()
        if not lowered.startswith(("http://", "https://", "webcal://", "webcals://")):
            raise ValueError("url must be an http(s):// or webcal(s):// URL")
        return u


class WakeCalendarUpdate(BaseModel):
    """PATCH body — every field optional. An explicit ``password: ""`` CLEARS
    the stored secret; omitting ``password`` leaves it unchanged; a non-empty
    string re-encrypts."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    kind: CalendarKind | None = None
    url: str | None = Field(default=None, min_length=1)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = None
    enabled: bool | None = None
    refresh_interval_minutes: int | None = Field(default=None, ge=5, le=10_080)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        u = v.strip()
        if not u.lower().startswith(("http://", "https://", "webcal://", "webcals://")):
            raise ValueError("url must be an http(s):// or webcal(s):// URL")
        return u


class WakeCalendarRead(BaseModel):
    """Calendar row for the wire. The CalDAV password is NEVER serialised —
    only ``password_set`` reveals whether one is stored."""

    id: uuid.UUID
    name: str
    kind: str
    url: str
    username: str | None
    password_set: bool
    enabled: bool
    refresh_interval_minutes: int
    last_synced_at: datetime | None
    last_sync_status: str | None
    last_sync_error: str | None
    event_count: int
    created_at: datetime
    modified_at: datetime


class CalendarEventRead(BaseModel):
    """One flattened all-day event span (recurrence already expanded)."""

    id: uuid.UUID
    starts_on: date
    ends_on: date
    summary: str | None
    categories: list[str]
    uid: str | None


class CalendarSyncResult(BaseModel):
    """Outcome of a ``POST /calendars/{id}/sync-now`` refresh."""

    status: str
    added: int = 0
    removed: int = 0
    total: int = 0
    error: str | None = None
    last_synced_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None


__all__ = [
    "SelectorMode",
    "TargetSelectorIn",
    "WakeScheduleCreate",
    "WakeScheduleUpdate",
    "WakeScheduleRead",
    "WakeTargetRead",
    "SkippedTargetRead",
    "TargetPreviewRequest",
    "TargetPreviewRead",
    "WakeRunRead",
    "WakeRunTargetRead",
    "WakeRunDetailRead",
    "CalendarMode",
    "CalendarKind",
    "WakeCalendarCreate",
    "WakeCalendarUpdate",
    "WakeCalendarRead",
    "CalendarEventRead",
    "CalendarSyncResult",
]
