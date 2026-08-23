"""The beat heartbeat must be bounded, and must not lie about who failed (#925).

`/health/platform` reported `celery-beat` unhealthy indefinitely after a
slot upgrade on a 3-node appliance, while `celery-workers` stayed green.
Both halves of that were reproduced directly:

1. ``make_sync_redis`` with no ``socket_connect_timeout`` — which is what
   ``beat_tick`` used to pass — blocks **indefinitely** against a sentinel
   that resolves but does not answer (still blocked at 60 s; ~28 s to a
   clean ``MasterNotFoundError`` once bounded). ``REDIS_URL`` on a
   multi-node control plane deliberately lists sentinels by per-pod
   headless DNS, and those names keep resolving through the 20-40 s a
   rebooting node takes to be marked NotReady — so a slot upgrade walks
   straight into it while a single-node install never does.

2. ``inspect ping`` answers ``pong`` with **every** prefork slot blocked
   (verified against a 2-slot worker holding two forever-tasks). So a
   worker whose pool is wedged reports healthy, and the only component
   that goes red is the one named after the innocent process.

A tick is enqueued every 30 s regardless, so one unbounded tick per
interval takes a 4-slot pool in ~2 minutes — which is exactly the "still
unhealthy after 120s" the report measured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.tasks.heartbeat import (
    BEAT_HEARTBEAT_INTERVAL_SECONDS,
    BEAT_HEARTBEAT_KEY,
    BEAT_HEARTBEAT_TTL_SECONDS,
    beat_tick,
)


def test_tick_cannot_outlive_its_own_interval_by_much() -> None:
    """A tick that never returns wedges one pool slot per interval.

    The limits are what turn "the worker pool is gone in two minutes and
    never comes back" into "this tick failed, the next one tries again".
    """
    soft = beat_tick.soft_time_limit
    hard = beat_tick.time_limit
    assert soft is not None and hard is not None, "an unbounded tick is the bug"
    assert soft < hard, "the soft limit must fire first so the task exits cleanly"
    assert hard <= BEAT_HEARTBEAT_INTERVAL_SECONDS, (
        "a tick allowed to outlive its interval still accumulates one wedged "
        "slot per interval, just more slowly"
    )


def test_expires_is_not_set() -> None:
    """``expires`` would trade this bug for a worse one.

    Celery stamps it as an *absolute* timestamp from the **publisher's**
    clock (beat) and ``Request.maybe_expire`` compares it against the
    **worker's** clock — verified against the installed celery. A worker
    node running ahead of beat by more than the window revokes every tick,
    so the key is never written and ``/health/platform`` sits red
    permanently: the reported symptom, made unconditional, in exactly the
    post-reboot window where NTP has not converged.

    The backlog it would prevent is harmless — every tick writes the same
    key, and the time limits above stop the pool wedging, so a backlog
    drains in seconds.
    """
    assert beat_tick.expires is None


def test_limits_clear_a_realistic_sentinel_outage() -> None:
    """A rolling node reboot leaves one sentinel resolving-but-dead.

    Measured cost of walking past one: ~12.8 s at the 1 s connect timeout
    this task uses (~17.5 s at 2 s) — far more than the connect timeout,
    because redis-py retries internally. A soft limit that does not clear
    that comfortably kills the tick just before it would have succeeded,
    degrading celery-beat for the length of every upgrade.
    """
    assert beat_tick.soft_time_limit >= 20


def test_redis_failure_is_swallowed_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis outage must not raise out of this task.

    The health endpoint already reports the outage from the key's absence.
    Raising would file a diagnostics row every 30 s for the duration —
    burying the signal under its own symptom.
    """

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ConnectionError("sentinel unreachable")

    monkeypatch.setattr("app.tasks.heartbeat.make_sync_redis", _boom)
    assert beat_tick.run() == "unavailable"


def test_client_that_fails_mid_write_is_still_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old body closed in a ``finally`` too, but only because the
    constructor sat outside the ``try``. Keep the close proven."""
    closed: list[bool] = []

    class _Client:
        def set(self, *_a: object, **_k: object) -> None:
            raise ConnectionError("dropped mid-write")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("app.tasks.heartbeat.make_sync_redis", lambda *a, **k: _Client())
    assert beat_tick.run() == "unavailable"
    assert closed == [True], "a client leaked on the failure path"


def test_happy_path_writes_a_parseable_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The health check does ``datetime.fromisoformat`` on this value and
    subtracts it from now, so the format is a contract between two files."""
    written: dict[str, object] = {}

    class _Client:
        def set(self, key: str, value: str, ex: int | None = None) -> None:
            written.update(key=key, value=value, ex=ex)

        def close(self) -> None:
            pass

    monkeypatch.setattr("app.tasks.heartbeat.make_sync_redis", lambda *a, **k: _Client())
    assert beat_tick.run() == "ok"
    assert written["key"] == BEAT_HEARTBEAT_KEY
    assert written["ex"] == BEAT_HEARTBEAT_TTL_SECONDS

    parsed = datetime.fromisoformat(str(written["value"]))
    assert parsed.tzinfo is not None, "a naive stamp makes the age arithmetic explode"
    assert abs(datetime.now(UTC) - parsed) < timedelta(seconds=30)
