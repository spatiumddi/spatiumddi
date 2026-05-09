"""Health probe endpoints consumed by Docker/Kubernetes."""

import asyncio
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import structlog
from fastapi import APIRouter
from sqlalchemy import text
from starlette import status
from starlette.responses import JSONResponse

from app.db import AsyncSessionLocal

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict:
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """
    Readiness probe — checks DB and Redis connectivity.
    Returns 200 if ready, 503 with failed-check details if not.
    """
    checks: dict[str, str] = {}
    healthy = True

    # Database check
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness_check_failed", check="database", error=str(exc))
        checks["database"] = f"error: {exc}"
        healthy = False

    # Redis check
    try:
        import redis.asyncio as aioredis

        from app.config import settings

        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
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
        import redis.asyncio as aioredis

        from app.config import settings

        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
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
