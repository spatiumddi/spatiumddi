"""Celery beat heartbeat.

A trivial task the beat scheduler fires every 30 s. It writes the
current UTC timestamp to a redis key with a 5-minute TTL. The
platform-health endpoint reads that key to distinguish "beat is
running" from "beat has stalled" — celery has no built-in beat-
liveness primitive, so a self-pinged heartbeat is the simplest
reliable signal.
"""

from __future__ import annotations

from datetime import UTC, datetime

import redis
import structlog

from app.celery_app import celery_app
from app.config import settings

logger = structlog.get_logger(__name__)

BEAT_HEARTBEAT_KEY = "spatium:beat:heartbeat"
BEAT_HEARTBEAT_TTL_SECONDS = 300


@celery_app.task(
    name="app.tasks.heartbeat.beat_tick",
    bind=True,
    ignore_result=True,
)
def beat_tick(self) -> str:  # noqa: ARG001 — bind=True boilerplate
    r = redis.from_url(settings.redis_url)
    try:
        r.set(
            BEAT_HEARTBEAT_KEY,
            datetime.now(UTC).isoformat(),
            ex=BEAT_HEARTBEAT_TTL_SECONDS,
        )
    finally:
        r.close()
    return "ok"


__all__ = ["beat_tick", "BEAT_HEARTBEAT_KEY", "BEAT_HEARTBEAT_TTL_SECONDS"]
