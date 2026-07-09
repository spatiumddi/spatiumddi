"""Holiday / term / calendar gate for Scheduled Wake-on-LAN (issue #586).

The built-in gate (Phase 1) is the schedule's own ``blackout_dates`` list +
``active_from`` / ``active_until`` term range, evaluated on the **local** date
(the candidate fire instant converted into the schedule's IANA timezone).  Both
comparisons are on the local calendar date so a 07:00-local wake on a blackout
day is correctly suppressed regardless of the UTC offset.

Phase 2 layers an **external calendar** gate ON TOP of the built-in checks
(term → blackout → calendar, in that order — the built-in reasons win when both
would suppress). When a schedule pins a ``calendar_id`` and a non-``none``
``calendar_mode``, the schedule's cached :class:`WolCalendarEvent` spans are
consulted:

* ``skip_on_event``  — a matching event covering the local fire date SKIPS the
  wake (holiday calendar) → ``"calendar_event"``.
* ``only_on_event``  — the wake only fires when a matching event covers the
  local fire date (term / school-day calendar); no match → ``"no_calendar_event"``.

``calendar_match`` (optional regex, case-insensitive) narrows WHICH events count
— matched against the event summary + each category. A malformed regex is
treated as "no filter" (a bad operator regex must never wedge the gate).

The calendar step is pure over ``(candidate, schedule, calendar_events)``; the
runner + previews load the events via :func:`load_gate_calendar_events` and pass
the concrete list.  Phase-1 callers that pass ``calendar_events=None`` keep the
calendar step a no-op.

Run-level skip reasons produced here:

* ``"off_term"``          — term range set + local date outside it.
* ``"holiday"``           — local date is a member of ``blackout_dates``.
* ``"calendar_event"``    — ``skip_on_event`` + a matching calendar event covers it.
* ``"no_calendar_event"`` — ``only_on_event`` + NO matching calendar event covers it.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.services.wol_scheduler.schedule import validate_timezone

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.wol_schedule import WolCalendarEvent, WolSchedule

# Run-level gate skip reasons (mirrored onto ``wol_run.skip_reason`` +
# ``wol_schedule.last_run_skip_reason``).
SKIP_OFF_TERM = "off_term"
SKIP_HOLIDAY = "holiday"
SKIP_CALENDAR_EVENT = "calendar_event"
SKIP_NO_CALENDAR_EVENT = "no_calendar_event"

# Calendar gate polarities (mirror ``wol_schedule.calendar_mode``).
CAL_MODE_NONE = "none"
CAL_MODE_SKIP_ON_EVENT = "skip_on_event"
CAL_MODE_ONLY_ON_EVENT = "only_on_event"


def _parse_iso_date(value: object) -> date | None:
    """Coerce a stored blackout entry into a :class:`date`.

    Accepts ``date`` objects and ISO ``YYYY-MM-DD`` strings; anything
    unparseable is ignored (a malformed operator entry must not wedge the
    gate — the sweep would rather fire than crash).
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def local_fire_date(candidate_utc: datetime, tz_name: str) -> date:
    """Return the local calendar date of ``candidate_utc`` in ``tz_name``.

    The gate compares against this local date, not the UTC date, so
    ``active_until`` / blackout membership line up with the operator's
    wall clock.
    """
    tz = validate_timezone(tz_name)
    anchor = (
        candidate_utc if candidate_utc.tzinfo is not None else candidate_utc.replace(tzinfo=UTC)
    )
    return anchor.astimezone(tz).date()


def _event_matches(event: WolCalendarEvent, pattern: re.Pattern[str] | None) -> bool:
    """Whether ``event`` counts under an optional ``calendar_match`` regex.

    ``None`` pattern → every event counts. Otherwise the regex must hit the
    event summary OR any of its categories (case-insensitive, substring via
    ``search``).
    """
    if pattern is None:
        return True
    if event.summary and pattern.search(event.summary):
        return True
    for cat in event.categories or []:
        if pattern.search(str(cat)):
            return True
    return False


def _compile_match(raw: str | None) -> re.Pattern[str] | None:
    """Compile ``calendar_match`` case-insensitively; a bad regex → ``None``
    (treated as "no filter" so a malformed operator entry can't wedge the gate)."""
    if not raw or not raw.strip():
        return None
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        return None


def _calendar_covers(
    local: date,
    events: Sequence[WolCalendarEvent],
    pattern: re.Pattern[str] | None,
) -> bool:
    """Whether any matching event's inclusive span covers ``local``."""
    for ev in events:
        if ev.starts_on <= local <= ev.ends_on and _event_matches(ev, pattern):
            return True
    return False


def gate_verdict(
    candidate_utc: datetime,
    schedule: WolSchedule,
    *,
    calendar_events: Sequence[WolCalendarEvent] | None = None,
) -> str | None:
    """Return a run-level skip reason, or ``None`` when the schedule is
    clear to fire at ``candidate_utc``.

    Evaluated on the local date in the schedule's timezone, in order:

    1. Term range set + local date outside ``[active_from, active_until]`` →
       ``"off_term"``.
    2. Local date in ``blackout_dates`` → ``"holiday"``.
    3. External calendar (Phase 2) — only when the schedule pins a
       ``calendar_id`` and a non-``none`` ``calendar_mode`` AND
       ``calendar_events`` is supplied (Phase-1 callers pass ``None`` → the
       calendar step is a no-op):
         * ``skip_on_event``  + a matching event covers the local date →
           ``"calendar_event"``.
         * ``only_on_event``  + NO matching event covers the local date →
           ``"no_calendar_event"``.
    4. Otherwise → ``None`` (fire).
    """
    local = local_fire_date(candidate_utc, schedule.timezone)

    if schedule.active_from is not None and local < schedule.active_from:
        return SKIP_OFF_TERM
    if schedule.active_until is not None and local > schedule.active_until:
        return SKIP_OFF_TERM

    for raw in schedule.blackout_dates or []:
        parsed = _parse_iso_date(raw)
        if parsed is not None and parsed == local:
            return SKIP_HOLIDAY

    mode = schedule.calendar_mode or CAL_MODE_NONE
    if schedule.calendar_id is not None and mode != CAL_MODE_NONE and calendar_events is not None:
        pattern = _compile_match(schedule.calendar_match)
        covered = _calendar_covers(local, calendar_events, pattern)
        if mode == CAL_MODE_SKIP_ON_EVENT and covered:
            return SKIP_CALENDAR_EVENT
        if mode == CAL_MODE_ONLY_ON_EVENT and not covered:
            return SKIP_NO_CALENDAR_EVENT

    return None


async def load_gate_calendar_events(
    db: AsyncSession, schedule: WolSchedule
) -> list[WolCalendarEvent] | None:
    """Load the cached event spans for a schedule's attached calendar, or
    ``None`` when no calendar gate is active (no ``calendar_id`` / mode
    ``none``) so the pure :func:`gate_verdict` calendar step no-ops.

    The event set is horizon-bounded (the reconciler only persists occurrences
    within ~400 days), so this is a small, single-index-hit query. The runner
    and the REST / MCP previews call this and pass the concrete list into
    :func:`gate_verdict`.
    """
    from app.models.wol_schedule import WolCalendarEvent as _Event  # noqa: PLC0415

    mode = schedule.calendar_mode or CAL_MODE_NONE
    if schedule.calendar_id is None or mode == CAL_MODE_NONE:
        return None
    return list(
        (await db.execute(select(_Event).where(_Event.calendar_id == schedule.calendar_id)))
        .scalars()
        .all()
    )


def evaluate_gate(
    schedule: WolSchedule,
    *,
    at: datetime | None = None,
) -> tuple[bool, str | None]:
    """Evaluate the built-in gate for ``schedule`` at ``at`` (default now).

    Returns ``(allowed, skip_reason)``: ``(True, None)`` when clear to fire,
    ``(False, "off_term" | "holiday")`` when the built-in gate suppresses the
    occurrence.  Thin ``bool``-returning wrapper over :func:`gate_verdict` for
    the runner's ``if not allowed: record skip`` branch.
    """
    candidate = at if at is not None else datetime.now(UTC)
    reason = gate_verdict(candidate, schedule)
    return (reason is None), reason
