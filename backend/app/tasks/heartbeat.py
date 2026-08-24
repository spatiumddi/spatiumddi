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
* the Redis connect timeout is deliberately tighter than the platform
  default. This is a 30 s-cadence liveness ping, not a data path: failing
  fast and retrying on the next tick beats waiting.

**``expires`` is deliberately NOT set.** It looks like the right way to
stop an outage backlog replaying, and it introduces a worse failure than
the one it prevents: Celery stamps ``expires`` as an *absolute* timestamp
from the **publisher's** clock (beat) and ``Request.maybe_expire``
compares it against the **worker's** clock. A worker node whose clock runs
ahead of beat's by more than the window revokes every tick, the key is
never written, and ``/health/platform`` sits red permanently — the exact
symptom this file exists to prevent, in exactly the post-reboot window
where NTP has not converged. The backlog it would have prevented is
harmless anyway: every tick writes the same key, and with the time limits
above the pool cannot wedge, so a backlog drains in seconds.

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

# Sized from measurement, not intuition. Walking past a sentinel that
# resolves but does not answer costs far more than its connect timeout,
# because redis-py retries internally: measured 17.5 s per dead sentinel at
# a 2 s connect timeout, 12.8 s at 1 s. A rolling node reboot leaves one
# sentinel in that state, so the limit has to clear ~13 s comfortably or it
# soft-kills the tick just before it would have succeeded — degrading
# celery-beat for the length of every upgrade, which is the #925 symptom
# rather than its fix.
#
# Both limits stay UNDER the 30 s schedule interval. That is the property
# that matters: a tick which can outlive its own interval still accumulates
# one occupied slot per interval, just more slowly.
_SOFT_TIME_LIMIT_SECONDS = 25
_HARD_TIME_LIMIT_SECONDS = 28

# Tighter than the platform default (2 s) on purpose — see the module
# docstring. At 1 s a dead sentinel costs ~12.8 s instead of ~17.5 s, which
# is what makes two of them fit inside the limits above.
_CONNECT_TIMEOUT_SECONDS = 1
_SOCKET_TIMEOUT_SECONDS = 2


@celery_app.task(
    name="app.tasks.heartbeat.beat_tick",
    bind=True,
    ignore_result=True,
    soft_time_limit=_SOFT_TIME_LIMIT_SECONDS,
    time_limit=_HARD_TIME_LIMIT_SECONDS,
)
def beat_tick(self) -> str:  # noqa: ARG001 — bind=True boilerplate
    r = None
    try:
        r = make_sync_redis(
            settings.redis_url,
            # Explicit rather than relying on the helper's default: this is
            # the caller whose missing timeout caused #925, and a reader
            # checking "is this one bounded?" should not have to go look.
            socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
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
