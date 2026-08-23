"""Celery beat heartbeat.

A trivial task the beat scheduler fires every 30 s. It writes the
current UTC timestamp to a redis key with a 5-minute TTL. The
platform-health endpoint reads that key to distinguish "beat is
running" from "beat has stalled" — celery has no built-in beat-
liveness primitive, so a self-pinged heartbeat is the simplest
reliable signal.

**It is a round trip, not a beat-side stamp**, and that matters when
reading the health verdict: beat *schedules* this task and a **worker**
executes it. A missing key therefore means "beat is not scheduling **or**
no worker ran the tick" — see ``app/api/health.py``.

#925 — this task is bounded on three axes because it used to be bounded
on none, and an unbounded heartbeat is worse than no heartbeat:

* the Redis client gets a connect timeout (now the helper's default);
* ``soft_time_limit`` stops one tick outliving its own 30 s interval and
  occupying a pool slot. Ticks are enqueued unconditionally every 30 s,
  so a tick that blocks forever wedges one slot per interval and takes
  the whole worker pool inside a few minutes — while ``inspect ping``,
  answered by the MainProcess, keeps reporting the worker healthy;
* ``expires`` discards a tick that has been queued too long instead of
  running a backlog of them after an outage, which would write the same
  key N times for no benefit.

A Redis failure is logged and swallowed rather than raised. The health
endpoint already reports the outage from the key's absence, and letting
this raise would file a diagnostics row every 30 s for the length of the
outage — burying the signal under the symptom.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from app.celery_app import celery_app
from app.config import settings
from app.core.redis_client import make_sync_redis

logger = structlog.get_logger(__name__)

BEAT_HEARTBEAT_KEY = "spatium:beat:heartbeat"
BEAT_HEARTBEAT_TTL_SECONDS = 300

# The schedule interval this task is registered at, in
# ``celery_app.beat_schedule["platform-beat-heartbeat"]``.
BEAT_HEARTBEAT_INTERVAL_SECONDS = 30

# Long enough for a sentinel round trip that has to walk past one dead
# sentinel (measured ~28 s against an unreachable one, so a tick during a
# node reboot is *expected* to hit this), short enough that a wedged tick
# frees its slot well inside the next few intervals.
_SOFT_TIME_LIMIT_SECONDS = 20
_HARD_TIME_LIMIT_SECONDS = 30

# Four intervals. Normal worker backlog never trips it; an outage backlog
# is discarded rather than replayed.
_EXPIRES_SECONDS = BEAT_HEARTBEAT_INTERVAL_SECONDS * 4


@celery_app.task(
    name="app.tasks.heartbeat.beat_tick",
    bind=True,
    ignore_result=True,
    soft_time_limit=_SOFT_TIME_LIMIT_SECONDS,
    time_limit=_HARD_TIME_LIMIT_SECONDS,
    expires=_EXPIRES_SECONDS,
)
def beat_tick(self) -> str:  # noqa: ARG001 — bind=True boilerplate
    r = None
    try:
        r = make_sync_redis(
            settings.redis_url,
            # Explicit rather than relying on the helper's default: this is
            # the caller whose missing timeout caused #925, and a reader
            # checking "is this one bounded?" should not have to go look.
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        r.set(
            BEAT_HEARTBEAT_KEY,
            datetime.now(UTC).isoformat(),
            ex=BEAT_HEARTBEAT_TTL_SECONDS,
        )
        return "ok"
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        logger.warning("beat_heartbeat_write_failed", error=str(exc))
        return "unavailable"
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001 — closing a broken client
                pass


__all__ = [
    "BEAT_HEARTBEAT_INTERVAL_SECONDS",
    "BEAT_HEARTBEAT_KEY",
    "BEAT_HEARTBEAT_TTL_SECONDS",
    "beat_tick",
]
