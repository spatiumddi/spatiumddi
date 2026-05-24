"""Health probe endpoints consumed by Docker/Kubernetes."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast

import structlog
from fastapi import APIRouter
from sqlalchemy import text
from starlette import status
from starlette.responses import JSONResponse

from app.db import AsyncSessionLocal

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])


# ── Schema-at-head cache (issue #299 phase 1) ──────────────────────
#
# The api image bundles a fixed set of alembic revisions, so the
# "expected head" is constant for the lifetime of the process — read
# it once, cache it, never re-read. On the appliance shape the
# migrate Job lands the head into the DB AFTER the api Deployment
# starts (CNPG bootstrap takes 1-2 min on a multi-node cluster), so
# without a schema-aware readiness probe the api passes /health/ready
# the moment Postgres accepts a SELECT 1 — even though every route
# handler then 500s against missing tables. The operator-visible
# symptom is a ~1-2 min window of bare nginx 502s on every login
# attempt (issue #299). Fixing /health/ready to require the schema
# at head makes the k8s readinessProbe accurately reflect "can serve
# traffic"; the readinessProbe controlling the Service endpoint set
# is what nginx upstream resolution depends on.
#
# Single dataclass instance instead of parallel scalars so the
# linter sees one used global instead of two it doesn't recognise
# via the ``global`` declaration. Same semantics — ``head`` is set
# once on success, ``error`` is set on "no head revision" config
# bugs (also persistent), and transient exceptions are NOT cached
# (the function re-tries on the next probe).
@dataclass(slots=True)
class _SchemaHeadCache:
    head: str | None = None
    error: str | None = None


_head_cache = _SchemaHeadCache()


def _expected_alembic_head() -> tuple[str | None, str | None]:
    """Read + cache the bundled alembic head.

    Returns ``(head, None)`` on success, ``(None, error_str)`` if the
    alembic.ini / scripts directory is missing or malformed.

    Cached at first call. Re-reading the script directory on every
    readiness probe call would be wasteful (and confusing during a
    multi-node rolling upgrade where the head DOES change between
    different api pods running different image tags — but each pod
    has its own image, so each pod's cache is correct for itself).
    """
    if _head_cache.head is not None or _head_cache.error is not None:
        return _head_cache.head, _head_cache.error
    try:
        # Same pattern as app/services/backup/migrations.py — the
        # alembic.ini lives at /app/alembic.ini inside the api
        # container image. ScriptDirectory.get_current_head() walks
        # the versions/ tree and returns the leaf revision (single-
        # head schemas only; multi-head environments aren't supported
        # by the SpatiumDDI shape).
        from alembic.config import Config  # noqa: PLC0415
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        cfg = Config(str(Path("/app/alembic.ini")))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        if head is None:
            _head_cache.error = "no head revision in script directory"
            return None, _head_cache.error
        _head_cache.head = head
        logger.info("readiness_schema_head_cached", expected_head=head)
        return head, None
    except Exception as exc:  # noqa: BLE001 — surface ANY exception
        # Don't cache transient errors — if the container's alembic
        # files genuinely missing this is a config bug operators need
        # to see; if it's a one-off blip, the next probe re-reads.
        msg = f"could not read alembic head: {exc}"
        logger.warning("readiness_schema_head_read_failed", error=str(exc))
        return None, msg


async def _check_schema_ready() -> tuple[str, str]:
    """Verify the DB schema is at the head this api image expects.

    Returns ``("ok", "<detail>")`` if the schema matches; otherwise
    ``("error", "<detail>")`` with a message the operator can act on
    ("migrate not run", "schema at X, image expects Y", etc.). The
    caller folds this into the readiness verdict alongside DB + Redis.

    Failure modes covered:

    * ``alembic_version`` table doesn't exist — the migrate Job hasn't
      created the schema at all. ProgrammingError / UndefinedTable
      from asyncpg surfaces as an exception; we map it to a 503 with
      "schema not initialised" so operators see the cause.
    * ``alembic_version`` row missing — alembic stamp / upgrade was
      interrupted. The api isn't ready; surface as 503.
    * ``version_num != expected_head`` — schema is behind (migrate
      still running, or a rolling upgrade started but didn't finish).
      Same 503 with the actual vs expected revisions so operators can
      compare.
    """
    expected, head_err = _expected_alembic_head()
    if head_err is not None:
        return "error", head_err
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT version_num FROM alembic_version"))
            row = result.fetchone()
    except Exception as exc:  # noqa: BLE001 — surface ANY exception
        # Most common shape here is asyncpg.exceptions.UndefinedTableError
        # ("relation 'alembic_version' does not exist") — migrate Job
        # hasn't run yet. Keep the error string short for the
        # operator-facing readiness response; full traceback is logged.
        logger.warning("readiness_schema_check_failed", error=str(exc), expected_head=expected)
        # Truncate "relation \"alembic_version\" does not exist" to a
        # one-liner the operator can match against migrate Job logs.
        short = str(exc).splitlines()[0][:160]
        return "error", f"schema not initialised: {short}"
    if row is None:
        return "error", "alembic_version row missing — migrate not stamped"
    actual = row[0]
    if actual != expected:
        return "error", f"schema at {actual}, image expects {expected}"
    return "ok", f"schema at head {expected}"


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict:
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """
    Readiness probe — checks DB connectivity, DB SCHEMA-at-head, and
    Redis connectivity. Returns 200 if ready, 503 with failed-check
    details if not.

    Schema-at-head is the key check for the cold-boot + post-restore
    + mid-rolling-upgrade windows where Postgres is up + accepting
    connections but the migrate Job hasn't landed the bundled
    alembic revisions yet. Without this check the api passes the
    readiness probe the moment a ``SELECT 1`` succeeds, gets added to
    the Service endpoint set, and serves 500s on every actual route
    handler until migrations finish — which the operator sees as
    bare nginx 502s through the frontend proxy (issue #299).
    """
    checks: dict[str, str] = {}
    healthy = True

    # Database connectivity check. Same SELECT 1 as before — separate
    # from the schema check so an operator looking at a 503 response
    # can tell "Postgres is down" apart from "Postgres is up but the
    # schema is behind."
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness_check_failed", check="database", error=str(exc))
        checks["database"] = f"error: {exc}"
        healthy = False

    # Schema-at-head check. Only run when the DB connect succeeded —
    # otherwise the schema check would surface a confusing
    # "could not connect" error on top of the database error above.
    if checks["database"] == "ok":
        verdict, detail = await _check_schema_ready()
        checks["schema"] = "ok" if verdict == "ok" else f"error: {detail}"
        if verdict != "ok":
            healthy = False

    # Redis check
    try:
        from app.config import settings
        from app.core.redis_client import make_async_redis

        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        await cast(Awaitable[bool], r.ping())
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("readiness_check_failed", check="redis", error=str(exc))
        checks["redis"] = f"error: {exc}"
        healthy = False

    body = {"status": "ok" if healthy else "degraded", "checks": checks}
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=body)


@router.get("/health/startup")
async def startup() -> JSONResponse:
    """Startup probe (Kubernetes slow-start containers) — same logic as readiness."""
    return await readiness()


@router.get("/health/platform")
async def platform_health() -> JSONResponse:
    """Dashboard-oriented rollup of every control-plane component.

    Distinct from ``/health/ready`` in that this one is authenticated-
    dashboard-facing rather than an orchestrator probe: it enumerates
    the individual pieces (db, redis, celery workers, celery beat)
    instead of returning a single binary verdict, so the UI can show a
    per-component status dot and explain what's wrong. Individual
    failures never make the endpoint itself fail — the caller always
    gets a 200 with the rollup. The top-level ``status`` folds the
    components into a single ``ok`` / ``degraded`` verdict for headline
    display.
    """
    components: list[dict[str, Any]] = [{"name": "api", "status": "ok", "detail": "responding"}]

    # Database
    t0 = monotonic()
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        components.append(
            {
                "name": "postgres",
                "status": "ok",
                "detail": f"SELECT 1 in {(monotonic() - t0) * 1000:.0f} ms",
            }
        )
    except Exception as exc:
        components.append({"name": "postgres", "status": "error", "detail": str(exc)})

    # Redis
    t0 = monotonic()
    try:
        from app.config import settings
        from app.core.redis_client import make_async_redis

        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        await cast(Awaitable[bool], r.ping())
        await r.aclose()
        components.append(
            {
                "name": "redis",
                "status": "ok",
                "detail": f"ping in {(monotonic() - t0) * 1000:.0f} ms",
            }
        )
    except Exception as exc:
        components.append({"name": "redis", "status": "error", "detail": str(exc)})

    # Celery workers — `inspect().ping()` is a sync, broker-backed RPC
    # that can hang, so run it in a threadpool with a short overall
    # timeout. Returns a mapping like ``{"celery@worker-1": {"ok": "pong"}}``
    # or ``None`` when no worker responds in time.
    def _inspect_ping() -> dict[str, Any] | None:
        from app.celery_app import celery_app  # noqa: PLC0415

        return celery_app.control.inspect(timeout=2).ping()

    try:
        ping = await asyncio.wait_for(asyncio.to_thread(_inspect_ping), timeout=3)
    except TimeoutError:
        ping = None
        workers_detail = "inspect timed out"
    except Exception as exc:  # noqa: BLE001
        ping = None
        workers_detail = f"inspect error: {exc}"
    else:
        workers_detail = None

    if ping:
        workers = sorted(ping.keys())
        components.append(
            {
                "name": "celery-workers",
                "status": "ok",
                "detail": f"{len(workers)} alive",
                "workers": workers,
            }
        )
    else:
        components.append(
            {
                "name": "celery-workers",
                "status": "error",
                "detail": workers_detail or "no workers responding",
                "workers": [],
            }
        )

    # Celery beat — written by ``app.tasks.heartbeat.beat_tick`` every 30 s
    # to ``spatium:beat:heartbeat`` with a 5-minute TTL. Missing key →
    # beat is stopped or has been stopped for >5 min. Present but older
    # than 90 s → degraded (two beat intervals missed).
    try:
        import redis.asyncio as aioredis

        from app.config import settings
        from app.tasks.heartbeat import BEAT_HEARTBEAT_KEY  # noqa: PLC0415

        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        raw = await r.get(BEAT_HEARTBEAT_KEY)
        await r.aclose()
        if raw is None:
            components.append(
                {
                    "name": "celery-beat",
                    "status": "error",
                    "detail": "no heartbeat — beat is stopped",
                }
            )
        else:
            ts_str = raw.decode() if isinstance(raw, bytes) else raw
            ts = datetime.fromisoformat(ts_str)
            age_s = (datetime.now(UTC) - ts).total_seconds()
            if age_s > 90:
                status_str = "warn"
                detail = f"last tick {age_s:.0f}s ago (stalled)"
            else:
                status_str = "ok"
                detail = f"last tick {age_s:.0f}s ago"
            components.append(
                {
                    "name": "celery-beat",
                    "status": status_str,
                    "detail": detail,
                    "last_tick": ts.isoformat(),
                }
            )
    except Exception as exc:
        components.append({"name": "celery-beat", "status": "error", "detail": str(exc)})

    rollup = "ok"
    for c in components:
        if c["status"] == "error":
            rollup = "degraded"
            break
        if c["status"] == "warn" and rollup == "ok":
            rollup = "degraded"

    # Surface demo-mode to the frontend so AppLayout can render a
    # persistent banner. Cheap to bundle here — every authenticated
    # page already polls /health/platform for the status dots, so we
    # avoid a separate round-trip on every page load.
    from app.config import settings as _settings

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": rollup,
            "components": components,
            "demo_mode": bool(_settings.demo_mode),
        },
    )
