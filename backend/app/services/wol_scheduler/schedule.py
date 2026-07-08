"""Cron parsing + DST-safe next-run computation for Scheduled
Wake-on-LAN — Phase 1 (issue #586).

Mirrors the backup scheduler's :mod:`app.services.backup.schedule`
shape, with one deliberate **delta**: a WoL schedule fires on the
operator's *wall clock* in a per-schedule IANA timezone and must
survive DST.  The fix is to localise the cron base into the
schedule's tz *before* handing it to :mod:`croniter` (croniter then
walks the wall clock in that zone), then denormalise the result back
to UTC for storage in the ``DateTime(timezone=True)`` ``next_run_at``
column so the SQL due-query stays a plain ``next_run_at <= now_utc``
compare.

Do NOT copy the backup helper verbatim — it hard-forces the base to
UTC (``base = after.astimezone(UTC)``), which would make a
``0 7 * * *`` schedule fire at 07:00 *UTC* and drift an hour across a
DST boundary.  We copy its *shape*, not its zone handling.

Three operations:

* :func:`compute_next_run` — next firing of a cron string in a given
  IANA tz, returned as tz-aware UTC.
* :func:`validate_cron` — 422-friendly parse check.
* :func:`validate_timezone` — 422-friendly IANA-zone check.
* :func:`is_due` — "should this schedule fire now?" predicate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter


class InvalidCronExpression(ValueError):
    """Raised when the operator-supplied cron string can't be parsed.

    The API layer 422s on it with the message verbatim so the operator
    sees what was wrong.
    """


class InvalidTimezone(ValueError):
    """Raised when the operator-supplied IANA timezone name is unknown."""


def validate_cron(expression: str) -> None:
    """Raise :class:`InvalidCronExpression` if ``expression`` isn't a
    valid 5-field cron string.
    """
    if not expression or not expression.strip():
        raise InvalidCronExpression("cron expression is empty")
    try:
        croniter(expression, datetime.now(UTC))
    except (CroniterBadCronError, ValueError, TypeError) as exc:
        raise InvalidCronExpression(f"invalid cron expression: {exc}") from exc


def validate_timezone(tz_name: str) -> ZoneInfo:
    """Return the :class:`ZoneInfo` for ``tz_name`` or raise
    :class:`InvalidTimezone`.

    Reuses the settings-router IANA validation shape so a schedule
    always localises against a concrete, resolvable zone.
    """
    if not tz_name or not tz_name.strip():
        raise InvalidTimezone("timezone is empty")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise InvalidTimezone(f"unknown IANA timezone: {tz_name!r}") from exc


def compute_next_run(
    expression: str,
    tz_name: str,
    *,
    after: datetime | None = None,
) -> datetime:
    """Return the next firing of ``expression`` strictly after ``after``,
    evaluated on the wall clock of the IANA zone ``tz_name`` and returned
    as tz-aware **UTC**.

    DST-safe: the base is localised into ``tz_name`` before croniter walks
    it, so ``0 2 * * *`` in ``America/New_York`` on a spring-forward day
    correctly skips the non-existent 02:00 and fires 03:00 local, and a
    ``0 7 * * *`` schedule always fires at 07:00 *local* regardless of the
    UTC offset that day.

    ``after`` defaults to now; a naive ``after`` is assumed to be UTC.
    """
    tz = validate_timezone(tz_name)
    base = after if after is not None else datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    # Localise the base into the schedule's wall-clock zone so croniter
    # advances against local time (and honours DST transitions), then
    # denormalise the tz-aware result back to UTC for storage.
    local_base = base.astimezone(tz)
    try:
        it = croniter(expression, local_base)
    except (CroniterBadCronError, ValueError, TypeError) as exc:
        raise InvalidCronExpression(f"invalid cron expression: {exc}") from exc
    return it.get_next(datetime).astimezone(UTC)


def is_due(next_run_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Cheap predicate for the beat sweep — "should this schedule fire
    right now?".

    A ``None`` ``next_run_at`` means "manual only" (never swept); a future
    ``next_run_at`` means "wait"; a past-or-equal ``next_run_at`` means
    "fire".  The SQL due-query does the same compare server-side.
    """
    if next_run_at is None:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC)
    anchor = next_run_at if next_run_at.tzinfo is not None else next_run_at.replace(tzinfo=UTC)
    return anchor.astimezone(UTC) <= current


# Plan-doc alias — some callers refer to the WoL-specific name.
compute_next_wol_run = compute_next_run
