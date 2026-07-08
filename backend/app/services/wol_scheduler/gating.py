"""Built-in holiday / term gate for Scheduled Wake-on-LAN — Phase 1
(issue #586).

Phase 1 has **no external iCal / CalDAV calendar** — the gate is the
schedule's own ``blackout_dates`` list + ``active_from`` / ``active_until``
term range, evaluated on the **local** date (the candidate fire instant
converted into the schedule's IANA timezone).  Both comparisons are on the
local calendar date so a 07:00-local wake on a blackout day is correctly
suppressed regardless of the UTC offset.

Pure functions over ``(candidate_utc, schedule)`` so they're trivially
unit-testable and the beat sweep stays a thin due-query + fire loop.

Run-level skip reasons produced here:

* ``"off_term"`` — a term range is set and the local date falls outside
  ``[active_from, active_until]``.
* ``"holiday"`` — the local date is a member of ``blackout_dates``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from app.services.wol_scheduler.schedule import validate_timezone

if TYPE_CHECKING:
    from app.models.wol_schedule import WolSchedule

# Run-level gate skip reasons (mirrored onto ``wol_run.skip_reason`` +
# ``wol_schedule.last_run_skip_reason``).
SKIP_OFF_TERM = "off_term"
SKIP_HOLIDAY = "holiday"


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


def gate_verdict(candidate_utc: datetime, schedule: WolSchedule) -> str | None:
    """Return a run-level skip reason, or ``None`` when the schedule is
    clear to fire at ``candidate_utc``.

    Evaluated on the local date in the schedule's timezone:

    1. If a term range is set and the local date is outside
       ``[active_from, active_until]`` → ``"off_term"``.
    2. If the local date is in ``blackout_dates`` → ``"holiday"``.
    3. Otherwise → ``None`` (fire).
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

    return None


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
